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

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
SHEET_NAME = os.getenv("SHEET_NAME", "Extratos")


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

class ExportRequest(BaseModel):
    transacoes: List[TransactionRow]


# ──────────────────────────────────────────────
# Mistral OCR
# ──────────────────────────────────────────────

SYSTEM_PROMPT = """És um sistema especializado em extrair dados de extratos bancários angolanos (formato BFA, BAI, BIC, Millennium Angola, BPA).
Devolve APENAS um JSON válido com a seguinte estrutura, sem markdown, sem texto extra, sem comentários:
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

REGRAS CRÍTICAS para extratos angolanos:
1. Extrai TODAS as linhas de transação — não saltes nenhuma
2. VALORES DÉBITO: nos extratos angolanos os débitos aparecem com "-" no final (ex: "14.300,00-" ou "14.300,-"). Remove o "-" final e coloca em "debito". Nunca coloques o "-" no valor
3. VALORES CRÉDITO: valores sem "-" na coluna crédito (ex: "2.000,00" ou "1.000,00"). Coloca em "credito"
4. IGNORA marcas manuscritas, visto (✓), carimbos, assinaturas e anotações à mão
5. IGNORA linhas de cabeçalho ("Saldo inicial", "A Transportar", "Total")
6. DATAS: formato AAAA-MM-DD ou DD-MM-AAAA — converte sempre para DD/MM/AAAA
7. SALDO: valor numérico sem "-", ex: "127.841,99"
8. TIPO DE MOVIMENTO: copia o texto exacto da coluna, incluindo referências como "TPA-MCX500294**35v35"
9. Se um campo não for visível ou não existir, usa string vazia ""
10. Valores monetários no formato angolano: ponto para milhar, vírgula para decimal (ex: 1.234.567,89)
11. A imagem pode ser uma fotografia inclinada ou de baixa qualidade — faz o melhor esforço para ler todos os dados
"""

async def extract_with_mistral(image_bytes: bytes, filename: str) -> ExtractionResult:
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    ext = filename.lower().split(".")[-1]
    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    mime = mime_map.get(ext, "image/jpeg")

    payload = {
        "model": "pixtral-12b-2409",
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"}
                    },
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT + "\n\nExtrai todas as transações desta imagem de extrato bancário angolano."
                    }
                ]
            }
        ]
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload
        )

    if response.status_code != 200:
        return ExtractionResult(
            ficheiro=filename,
            transacoes=[],
            erro=f"Mistral API erro {response.status_code}: {response.text[:200]}"
        )

    raw_text = response.json()["choices"][0]["message"]["content"]

    # Strip markdown fences if present
    clean = re.sub(r"```(?:json)?", "", raw_text).strip()

    try:
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
        return ExtractionResult(ficheiro=filename, transacoes=rows)
    except json.JSONDecodeError as e:
        return ExtractionResult(
            ficheiro=filename,
            transacoes=[],
            erro=f"Falha ao parsear JSON: {str(e)} | Raw: {raw_text[:300]}"
        )


# ──────────────────────────────────────────────
# Google Sheets
# ──────────────────────────────────────────────

HEADERS = ["Data Movimento", "Data Valor", "Tipo de Movimento", "Débito", "Crédito", "Saldo", "Ficheiro"]

def get_sheets_service():
    if not GOOGLE_CREDENTIALS_JSON:
        raise HTTPException(status_code=500, detail="GOOGLE_CREDENTIALS_JSON não configurado")

    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


def ensure_headers(service):
    """Garante que a primeira linha tem os cabeçalhos."""
    result = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"{SHEET_NAME}!A1:G1"
    ).execute()

    values = result.get("values", [])
    if not values or values[0] != HEADERS:
        service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=f"{SHEET_NAME}!A1",
            valueInputOption="RAW",
            body={"values": [HEADERS]}
        ).execute()


def append_rows(service, transacoes: List[TransactionRow]):
    rows = [
        [
            t.data_movimento,
            t.data_valor,
            t.tipo_movimento,
            t.debito,
            t.credito,
            t.saldo,
            t.ficheiro
        ]
        for t in transacoes
    ]
    service.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=f"{SHEET_NAME}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()


# ──────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


@app.post("/extract", response_model=List[ExtractionResult])
async def extract_images(files: List[UploadFile] = File(...)):
    """Recebe múltiplas imagens e extrai transações via Mistral OCR."""
    if not MISTRAL_API_KEY:
        raise HTTPException(status_code=500, detail="MISTRAL_API_KEY não configurado")

    if len(files) > 20:
        raise HTTPException(status_code=400, detail="Máximo 20 imagens por vez")

    results = []
    for file in files:
        content = await file.read()
        result = await extract_with_mistral(content, file.filename)
        results.append(result)

    return results


@app.post("/export")
async def export_to_sheets(body: ExportRequest):
    """Faz append das transações no Google Sheets."""
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
    """Extrai e exporta directamente num único passo."""
    results = await extract_images(files)

    all_transactions = []
    for r in results:
        all_transactions.extend(r.transacoes)

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
