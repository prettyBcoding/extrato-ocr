import os
import base64
import json
import re
import httpx
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google.cloud import documentai
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Extrato OCR API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MISTRAL_API_KEY         = os.getenv("MISTRAL_API_KEY")
GOOGLE_SHEET_ID         = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
SHEET_NAME              = os.getenv("SHEET_NAME", "Extratos")
DOCUMENTAI_PROJECT_ID   = os.getenv("DOCUMENTAI_PROJECT_ID", "n8n-458109")
DOCUMENTAI_PROCESSOR_ID = os.getenv("DOCUMENTAI_PROCESSOR_ID", "1b6ece8dd285d5e2")
DOCUMENTAI_LOCATION     = os.getenv("DOCUMENTAI_LOCATION", "eu")


# ──────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────

class TransactionRow(BaseModel):
    data_movimento: Optional[str] = ""
    data_valor: Optional[str] = ""
    tipo_movimento: Optional[str] = ""
    debito: Optional[str] = ""
    credito: Optional[str] = ""
    saldo: Optional[str] = ""
    ficheiro: Optional[str] = ""

class ExtractionResult(BaseModel):
    ficheiro: str
    transacoes: List[TransactionRow]
    erro: Optional[str] = None
    texto_ocr: Optional[str] = None

class ExportRequest(BaseModel):
    transacoes: List[TransactionRow]


# ──────────────────────────────────────────────
# Google Document AI — extrai texto bruto
# ──────────────────────────────────────────────

def get_documentai_client():
    if not GOOGLE_CREDENTIALS_JSON:
        raise Exception("GOOGLE_CREDENTIALS_JSON não configurado")
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    opts = {"api_endpoint": f"{DOCUMENTAI_LOCATION}-documentai.googleapis.com"}
    return documentai.DocumentProcessorServiceClient(
        credentials=creds,
        client_options=opts
    )


def ocr_with_documentai(image_bytes: bytes, filename: str) -> str:
    client = get_documentai_client()
    processor_name = client.processor_path(
        DOCUMENTAI_PROJECT_ID,
        DOCUMENTAI_LOCATION,
        DOCUMENTAI_PROCESSOR_ID
    )
    ext = filename.lower().split(".")[-1]
    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "pdf": "application/pdf"}
    mime_type = mime_map.get(ext, "image/jpeg")

    raw_document = documentai.RawDocument(content=image_bytes, mime_type=mime_type)
    request = documentai.ProcessRequest(name=processor_name, raw_document=raw_document)
    result = client.process_document(request=request)
    return result.document.text


# ──────────────────────────────────────────────
# Mistral — estrutura texto OCR em JSON
# ──────────────────────────────────────────────

STRUCTURE_PROMPT = """És um sistema especializado em extrair dados estruturados de texto OCR de extratos bancários angolanos (BFA, BAI, BIC, Millennium Angola, BPA).

Recebes o texto bruto extraído por OCR. Identifica todas as transações e devolve APENAS um JSON válido, sem markdown, sem texto extra, sem comentários.

Formato de saída:
{
  "transacoes": [
    {
      "data_movimento": "DD/MM/AAAA",
      "data_valor": "DD/MM/AAAA",
      "tipo_movimento": "descrição do movimento",
      "debito": "valor em AOA ou vazio",
      "credito": "valor em AOA ou vazio",
      "saldo": "valor em AOA"
    }
  ]
}

REGRAS CRÍTICAS:
1. Extrai TODAS as linhas de transação — não saltes nenhuma
2. DÉBITOS: terminam com "-" (ex: "14.300,00-" ou "630,-"). Remove o "-" e coloca em "debito"
3. CRÉDITOS: valores sem "-". Coloca em "credito"
4. IGNORA: cabeçalhos, "Saldo inicial", "A Transportar", "Total", "NBA", "IBAN", "Agência"
5. DATAS: converte sempre para DD/MM/AAAA (ex: 2023-01-02 → 02/01/2023)
6. SALDO: valor positivo sem "-" (ex: "127.841,99")
7. TIPO DE MOVIMENTO: texto exacto incluindo códigos (ex: "TPA-MCX500294**35v35 SHOPRITE")
8. Campo inexistente = string vazia ""
9. Formato monetário: ponto milhar, vírgula decimal (ex: 1.234.567,89)
10. Cada linha com duas datas seguidas é uma transação
"""

async def structure_with_mistral(raw_text: str, filename: str) -> List[TransactionRow]:
    payload = {
        "model": "mistral-large-latest",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": f"{STRUCTURE_PROMPT}\n\nTexto OCR:\n\n{raw_text}"}]
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {MISTRAL_API_KEY}", "Content-Type": "application/json"},
            json=payload
        )
    if response.status_code != 200:
        raise Exception(f"Mistral API erro {response.status_code}: {response.text[:200]}")

    raw_response = response.json()["choices"][0]["message"]["content"]
    clean = re.sub(r"```(?:json)?", "", raw_response).strip()
    data = json.loads(clean)
    rows = []
    for t in data.get("transacoes", []):
        rows.append(TransactionRow(
            data_movimento=t.get("data_movimento", ""),
            data_valor=t.get("data_valor", ""),
            tipo_movimento=t.get("tipo_movimento", ""),
            debito=t.get("debito", ""),
            credito=t.get("credito", ""),
            saldo=t.get("saldo", ""),
            ficheiro=filename
        ))
    return rows


# ──────────────────────────────────────────────
# Pipeline: Document AI → Mistral
# ──────────────────────────────────────────────

async def extract_from_image(image_bytes: bytes, filename: str) -> ExtractionResult:
    try:
        raw_text = ocr_with_documentai(image_bytes, filename)
    except Exception as e:
        return ExtractionResult(ficheiro=filename, transacoes=[], erro=f"Document AI erro: {str(e)}")

    if not raw_text or len(raw_text.strip()) < 20:
        return ExtractionResult(ficheiro=filename, transacoes=[], erro="Document AI não extraiu texto suficiente")

    try:
        transacoes = await structure_with_mistral(raw_text, filename)
        return ExtractionResult(ficheiro=filename, transacoes=transacoes, texto_ocr=raw_text[:500])
    except json.JSONDecodeError as e:
        return ExtractionResult(ficheiro=filename, transacoes=[], erro=f"Erro JSON Mistral: {str(e)}", texto_ocr=raw_text[:500])
    except Exception as e:
        return ExtractionResult(ficheiro=filename, transacoes=[], erro=f"Mistral erro: {str(e)}", texto_ocr=raw_text[:500])


# ──────────────────────────────────────────────
# Google Sheets
# ──────────────────────────────────────────────

HEADERS = ["Data Movimento", "Data Valor", "Tipo de Movimento", "Débito", "Crédito", "Saldo", "Ficheiro"]

def get_sheets_service():
    if not GOOGLE_CREDENTIALS_JSON:
        raise HTTPException(status_code=500, detail="GOOGLE_CREDENTIALS_JSON não configurado")
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds)

def ensure_headers(service):
    result = service.spreadsheets().values().get(spreadsheetId=GOOGLE_SHEET_ID, range=f"{SHEET_NAME}!A1:G1").execute()
    values = result.get("values", [])
    if not values or values[0] != HEADERS:
        service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID, range=f"{SHEET_NAME}!A1",
            valueInputOption="RAW", body={"values": [HEADERS]}
        ).execute()

def append_rows(service, transacoes: List[TransactionRow]):
    rows = [[t.data_movimento, t.data_valor, t.tipo_movimento, t.debito, t.credito, t.saldo, t.ficheiro] for t in transacoes]
    service.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID, range=f"{SHEET_NAME}!A1",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()


# ──────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0", "pipeline": "DocumentAI + Mistral"}

@app.post("/extract", response_model=List[ExtractionResult])
async def extract_images(files: List[UploadFile] = File(...)):
    if not MISTRAL_API_KEY:
        raise HTTPException(status_code=500, detail="MISTRAL_API_KEY não configurado")
    if not GOOGLE_CREDENTIALS_JSON:
        raise HTTPException(status_code=500, detail="GOOGLE_CREDENTIALS_JSON não configurado")
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="Máximo 20 imagens por vez")
    results = []
    for file in files:
        content = await file.read()
        result = await extract_from_image(content, file.filename)
        results.append(result)
    return results

@app.post("/export")
async def export_to_sheets(body: ExportRequest):
    if not body.transacoes:
        raise HTTPException(status_code=400, detail="Nenhuma transação para exportar")
    try:
        service = get_sheets_service()
        ensure_headers(service)
        append_rows(service, body.transacoes)
        return {"sucesso": True, "linhas_exportadas": len(body.transacoes)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/extract-and-export", response_model=List[ExtractionResult])
async def extract_and_export(files: List[UploadFile] = File(...)):
    results = await extract_images(files)
    all_transactions = [t for r in results for t in r.transacoes]
    if all_transactions:
        try:
            service = get_sheets_service()
            ensure_headers(service)
            append_rows(service, all_transactions)
        except Exception as e:
            for r in results:
                if not r.erro:
                    r.erro = f"Extração OK, mas falha no export: {str(e)}"
    return results
