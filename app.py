import asyncio
import json
import os
from threading import Lock

import gradio as gr
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from email_validator import validate_email, EmailNotValidError

USER_DB_FILE = "users.json"
user_db_lock = Lock()
SEMAPHORE_LIMIT = 5
semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)

def load_users():
    if not os.path.exists(USER_DB_FILE):
        default = {
            "demo": {
                "kalan_kredi": 10,
                "rol": "user",
                "gemini_key": "",
                "gsheets_creds": ""
            }
        }
        save_users(default)
        return default
    with open(USER_DB_FILE, "r") as f:
        return json.load(f)

def save_users(data):
    with user_db_lock:
        with open(USER_DB_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

def get_user_settings(user_key):
    users = load_users()
    return users.get(user_key, None)

def save_user_settings(user_key, gemini_key, gsheets_creds):
    if not user_key or not gemini_key or not gsheets_creds:
        return "❌ Tüm alanlar zorunlu!"
    try:
        json.loads(gsheets_creds)
    except:
        return "❌ Sheets JSON geçerli değil!"
    users = load_users()
    if user_key not in users:
        return "❌ Geçersiz kullanıcı anahtarı!"
    users[user_key]["gemini_key"] = gemini_key
    users[user_key]["gsheets_creds"] = gsheets_creds
    save_users(users)
    return "✅ Ayarlar kaydedildi!"

def check_and_deduct(user_key, kredi):
    users = load_users()
    u = users.get(user_key)
    if not u or u["kalan_kredi"] < kredi:
        return False
    u["kalan_kredi"] -= kredi
    save_users(users)
    return True

def get_gsheet_client(user_key):
    settings = get_user_settings(user_key)
    if not settings or not settings["gsheets_creds"]:
        raise Exception("Sheets ayarlanmamış.")
    creds_dict = json.loads(settings["gsheets_creds"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

def write_to_sheet(user_key, row):
    client = get_gsheet_client(user_key)
    sheet_name = "SessizOperator_Leads"
    try:
        spreadsheet = client.open(sheet_name)
        sheet = spreadsheet.sheet1
    except gspread.SpreadsheetNotFound:
        spreadsheet = client.create(sheet_name)
        sheet = spreadsheet.sheet1
        sheet.append_row(["URL", "E-posta", "Şirket Özeti", "Buzkıran", "Özel Filtre"])
        sheet.freeze(rows=1)
        sheet.format("C:E", {"wrapStrategy": "WRAP"})
        try:
            spreadsheet.batch_update({
                "requests": [{
                    "addBanding": {
                        "bandedRange": {
                            "range": {
                                "sheetId": sheet.id,
                                "startRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": 5
                            },
                            "bandingId": 1,
                            "bandingProperties": {
                                "headerColor": {"red": 0.2, "green": 0.2, "blue": 0.2, "alpha": 1.0},
                                "firstBandColor": {"red": 0.95, "green": 0.95, "blue": 0.95, "alpha": 1.0},
                                "secondBandColor": {"red": 1.0, "green": 1.0, "blue": 1.0, "alpha": 1.0}
                            }
                        }
                    }
                }]
            })
        except:
            pass
    sheet.append_row(row)
    return True

def extract_with_gemini(text, ozel_filtre, user_key):
    settings = get_user_settings(user_key)
    if not settings or not settings["gemini_key"]:
        return {"email": "HATA", "ozet": "Gemini anahtarı yok", "ice_breaker": ""}
    genai.configure(api_key=settings["gemini_key"])
    model = genai.GenerativeModel(
        "gemini-1.5-flash",
        safety_settings={
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
    )
    prompt = f"""
Sen bir iş istihbarat asistanısın. Aşağıdaki metinden şirketin iletişim e-postasını, 
1 cümlelik ne iş yaptıklarını ve onlara satış maili atarken kullanılabilecek kişiselleştirilmiş 
bir 'Ice-breaker' (buzkıran) cümlesi çıkar. 

Kesinlikle sadece JSON formatında cevap ver:
{{"email": "...", "ozet": "...", "ice_breaker": "..."}}

ÖNEMLİ KURALLAR:
- Eğer metinde e-posta yoksa email alanına "BULUNAMADI" yaz.
- Şirket hakkında net bilgi yoksa ozet için "BİLGİ YOK" yaz.
- Eğer özel filtre varsa ve şirket bu filtreye uymuyorsa email alanına "UYUMSUZ" yaz.
- ASLA e-posta veya bilgi uydurma.

Özel filtre: {ozel_filtre if ozel_filtre else "Yok"}

Metin:
{text[:15000]}
"""
    try:
        response = model.generate_content(prompt)
        raw = response.text.strip()
        if raw.startswith("```json"): raw = raw[7:]
        if raw.endswith("```"): raw = raw[:-3]
        return json.loads(raw)
    except:
        return {"email": "HATA", "ozet": "AI yanıt vermedi", "ice_breaker": ""}

async def crawl_site(url):
    async with semaphore:
        try:
            async with AsyncWebCrawler() as crawler:
                config = CrawlerRunConfig(max_pages=2)
                result = await crawler.arun(url, config=config)
                return result.markdown if result else ""
        except:
            return ""

async def process_url(url, ozel_filtre, user_key):
    text = await crawl_site(url)
    if not text:
        return [url, "", "Taranamadı", "", ozel_filtre]
    
    info = extract_with_gemini(text, ozel_filtre, user_key)
    email = info.get("email", "HATA")
    ozet = info.get("ozet", "")
    ice = info.get("ice_breaker", "")
    
    if email in ["BULUNAMADI", "UYUMSUZ", "HATA"]:
        return [url, email, ozet, ice, ozel_filtre]
    try:
        validate_email(email)
    except:
        return [url, "GEÇERSİZ", ozet, ice, ozel_filtre]
    
    try:
        write_to_sheet(user_key, [url, email, ozet, ice, ozel_filtre])
    except Exception as e:
        return [url, email, ozet, ice, f"Sheets hatası: {str(e)}"]
    
    return [url, email, ozet, ice, ozel_filtre]

async def run_engine(user_key, url_list_str, paket, ozel_filtre, progress=gr.Progress()):
    settings = get_user_settings(user_key)
    if not settings or not settings["gemini_key"] or not settings["gsheets_creds"]:
        return gr.Dataframe(headers=["Hata"], values=[["Lütfen önce Ayarlar sekmesinden API anahtarlarınızı girin."]])
    if settings["kalan_kredi"] <= 0:
        return gr.Dataframe(headers=["Hata"], values=[["Krediniz bitmiş."]])
    
    urls = [u.strip() for u in url_list_str.split("\n") if u.strip()]
    if not urls:
        return gr.Dataframe(headers=["Hata"], values=[["Lütfen en az bir URL girin."]])
    
    paket_limits = {"50 Lead": 50, "250 Lead": 250, "1000 Lead": 1000}
    max_leads = paket_limits.get(paket, 50)
    urls = urls[:max_leads]
    
    birim_kredi = 2 if ozel_filtre else 1
    toplam_kredi = len(urls) * birim_kredi
    
    if not check_and_deduct(user_key, toplam_kredi):
        return gr.Dataframe(headers=["Hata"], values=[[f"Yetersiz kredi. İhtiyacınız: {toplam_kredi}"]])
    
    results = []
    tasks = [process_url(u, ozel_filtre, user_key) for u in urls]
    for i, coro in enumerate(asyncio.as_completed(tasks)):
        row = await coro
        results.append(row)
        progress((i+1)/len(urls), desc=f"{i+1}/{len(urls)} site bitti")
    
    header = ["URL", "E-posta", "Şirket Özeti", "Buzkıran", "Özel Filtre"]
    return gr.Dataframe(headers=header, values=results)

with gr.Blocks(theme=gr.themes.Soft(), title="Sessiz Operatör") as demo:
    gr.Markdown("# 🕵️ Sessiz Operatör – B2B Lead Kazıma (Profesyonel)")
    with gr.Tabs():
        with gr.TabItem("🔍 Lead Kazı"):
            user_key_main = gr.Textbox(label="Kullanıcı Anahtarınız", placeholder="demo")
            url_box = gr.Textbox(label="URL Listesi (alt alta)", lines=6, placeholder="https://ornek.com")
            with gr.Row():
                paket = gr.Dropdown(["50 Lead", "250 Lead", "1000 Lead"], value="50 Lead", label="Paket")
            ozel_filtre = gr.Textbox(label="Özel İstek Filtresi (opsiyonel, 2 kat kredi yakar)")
            start_btn = gr.Button("🚀 Başlat", variant="primary")
            output_df = gr.Dataframe(label="Sonuçlar", interactive=False)
            start_btn.click(
                fn=run_engine,
                inputs=[user_key_main, url_box, paket, ozel_filtre],
                outputs=output_df
            )
        with gr.TabItem("⚙️ Ayarlar"):
            gr.Markdown("## API Anahtarlarınızı Buraya Yapıştırın")
            user_key_set = gr.Textbox(label="Kullanıcı Anahtarınız", placeholder="demo")
            gemini_key = gr.Textbox(label="Gemini API Anahtarı", type="password")
            sheets_json = gr.Textbox(label="Google Sheets Servis Hesabı JSON", lines=6, placeholder="{}")
            save_btn = gr.Button("💾 Kaydet", variant="secondary")
            msg = gr.Textbox(label="Sonuç", interactive=False)
            save_btn.click(
                fn=save_user_settings,
                inputs=[user_key_set, gemini_key, sheets_json],
                outputs=msg
            )
    gr.Markdown("---\n🔒 Tüm anahtarlar sunucuda şifresiz saklanır. Sadece size ait bulut sunucusunda çalışır.")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    demo.queue().launch(server_name="0.0.0.0", server_port=port)
