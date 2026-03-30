# 📄 Extrato OCR · Angola

Extrai automaticamente dados de extratos bancários angolanos (AOA) via OCR e exporta para Google Sheets.

**Stack:** FastAPI · Mistral Pixtral (OCR gratuito) · Google Sheets API · React (frontend HTML)

---

## Arquitectura

```
Frontend (index.html)
    │  upload de imagens (JPG/PNG/WEBP, até 20)
    ▼
Backend (FastAPI / Render)
    ├─► POST /extract   → Mistral Pixtral API (OCR gratuito)
    └─► POST /export    → Google Sheets API (append)
```

---

## 1. Pré-requisitos

- Python 3.11+
- Conta Mistral AI (gratuita): https://console.mistral.ai
- Conta Google Cloud com Sheets API activada

---

## 2. Configurar Mistral AI

1. Acede a https://console.mistral.ai/api-keys
2. Cria uma API Key (o tier gratuito suporta Pixtral-12b)
3. Guarda a chave — vais colocá-la em `MISTRAL_API_KEY`

---

## 3. Configurar Google Sheets API

### 3.1 Criar Service Account

1. Vai ao [Google Cloud Console](https://console.cloud.google.com)
2. Cria um projecto (ou usa um existente)
3. Activa a **Google Sheets API**: APIs & Services → Library → "Google Sheets API" → Enable
4. Vai a **APIs & Services → Credentials → Create Credentials → Service Account**
5. Dá um nome (ex: `extrato-ocr`) e clica em **Done**
6. Clica na service account criada → **Keys** → **Add Key → JSON**
7. Faz download do ficheiro JSON

### 3.2 Preparar a spreadsheet

1. Abre o teu Google Sheets
2. Cria um separador chamado `Extratos` (ou o nome que definires em `SHEET_NAME`)
3. Copia o ID da spreadsheet do URL:
   ```
   https://docs.google.com/spreadsheets/d/SEU_SHEET_ID_AQUI/edit
   ```
4. Partilha a spreadsheet com o **email da service account** (ex: `extrato-ocr@projecto.iam.gserviceaccount.com`) com permissão **Editor**

### 3.3 Converter JSON de credenciais para uma linha

O ficheiro JSON precisa de ser colocado numa variável de ambiente numa só linha:

```bash
# Linux/Mac
cat credentials.json | tr -d '\n'

# Ou simplesmente abre o ficheiro e remove quebras de linha manualmente
```

---

## 4. Deploy no Render (recomendado)

### 4.1 Subir código

```bash
git init
git add .
git commit -m "first commit"
git remote add origin https://github.com/SEU_USER/extrato-ocr.git
git push -u origin main
```

### 4.2 Criar Web Service no Render

1. Vai a https://render.com → New → Web Service
2. Conecta o teu repositório GitHub
3. Configurações:
   - **Root Directory:** `backend`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type:** Free

### 4.3 Variáveis de ambiente no Render

Em **Environment → Add Environment Variable**:

| Chave | Valor |
|-------|-------|
| `MISTRAL_API_KEY` | A tua chave Mistral |
| `GOOGLE_SHEET_ID` | ID da tua spreadsheet |
| `GOOGLE_CREDENTIALS_JSON` | Conteúdo do JSON da service account (numa linha) |
| `SHEET_NAME` | `Extratos` (ou o nome do teu separador) |

### 4.4 Obter URL do backend

Após deploy, o Render fornece um URL tipo:
```
https://extrato-ocr-api.onrender.com
```

---

## 5. Usar o Frontend

1. Abre o ficheiro `frontend/index.html` directamente no browser (não precisa de servidor)
2. No campo **Backend URL** no topo, coloca o URL do Render:
   ```
   https://extrato-ocr-api.onrender.com
   ```
3. Arrasta as imagens de extrato (JPG/PNG, até 20)
4. Clica **⚡ Extrair Dados** — aguarda o OCR
5. Revê e edita os dados na tabela se necessário
6. Clica **📊 Exportar para Google Sheets**

> **Nota:** O frontend pode também ser hospedado gratuitamente no GitHub Pages ou Netlify Drop.

---

## 6. Estrutura de colunas no Google Sheets

O backend garante automaticamente que a primeira linha tem os cabeçalhos:

| Data Movimento | Data Valor | Tipo de Movimento | Débito | Crédito | Saldo | Ficheiro |
|---|---|---|---|---|---|---|
| 15/03/2024 | 15/03/2024 | TRF PARA FULANO | | 50 000,00 | 1 234 567,89 | extrato_mar.jpg |

---

## 7. Testar localmente

```bash
cd backend
pip install -r requirements.txt

# Cria o ficheiro .env baseado no .env.example
cp .env.example .env
# Edita o .env com as tuas credenciais

uvicorn main:app --reload --port 8000
```

Endpoints disponíveis em `http://localhost:8000/docs` (Swagger UI automático).

---

## 8. Endpoints da API

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| GET | `/health` | Verifica se o backend está online |
| POST | `/extract` | Extrai transações das imagens (retorna JSON) |
| POST | `/export` | Exporta transações para Google Sheets |
| POST | `/extract-and-export` | Extrai e exporta em um único passo |

---

## 9. Limites & notas

- Máximo **20 imagens** por pedido
- Modelo OCR: **Pixtral-12b-2409** (gratuito no Mistral)
- Formato de valores esperado: `1.234.567,89` (padrão angolano/português)
- O frontend guarda o URL do backend no `localStorage` do browser

---

## Problemas comuns

**`403 Forbidden` no Google Sheets**
→ Verifica se partilhaste a spreadsheet com o email da service account com permissão Editor.

**`401 Unauthorized` no Mistral**
→ Verifica se `MISTRAL_API_KEY` está correcta e sem espaços.

**Nenhuma transação extraída**
→ A qualidade da imagem pode ser baixa. Tenta com imagem de maior resolução ou melhor contraste.

**Backend a dormir (cold start no Render free tier)**
→ O primeiro pedido pode demorar 30-60 segundos. Os seguintes são imediatos.
