from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import datetime
import os
import json
import openai
import pytesseract
from PIL import Image
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename
import re
import gspread
from google.oauth2.service_account import Credentials

def get_or_create_drive_folder(service, folder_name):
    # Check if folder already exists
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, spaces='drive', fields="files(id, name)").execute()
    items = results.get('files', [])

    if items:
        return items[0]['id']  # Folder exists, return ID

    # Folder doesn't exist, so create it
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder'
    }
    folder = service.files().create(body=file_metadata, fields='id').execute()
    return folder['id']

# Configuration
import os
openai.api_key = os.environ.get("OPENAI_API_KEY")

UPLOAD_FOLDER = "uploads"
SHEET_NAME = "EXPENSE LOG"  # your Google Sheet name
import os
CREDS_FILE = os.environ.get("GOOGLE_CREDS_PATH")

HEADERS = ["Vendor", "Date", "Amount", "Drive Link"]

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route("/scan-receipt", methods=["POST"])
def scan_receipt():
    if "file" not in request.files:
        return jsonify({"error": "No file part in the request"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    ext = os.path.splitext(file.filename)[1]  # e.g. ".jpg" or ".png"
    filename = f"receipt_{timestamp}{ext}"

    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)
    # Setup Google Drive service
    scope = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]
    drive_creds = Credentials.from_service_account_file(CREDS_FILE, scopes=scope)

    drive_creds = Credentials.from_service_account_file(CREDS_FILE, scopes=scope)
    drive_service = build('drive', 'v3', credentials=drive_creds)

    # Get or create "Receipt Images" folder
    folder_id = get_or_create_drive_folder(drive_service, "Receipt Images")

    # Upload file to Drive
    file_metadata = {
    'name': filename,
    'parents': [folder_id],
    }
    media = MediaFileUpload(filepath, mimetype='image/jpeg')
    uploaded = drive_service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
    # 🔓 Make it public to anyone with the link
    drive_service.permissions().create(
    fileId=uploaded['id'],
    body={
        'role': 'reader',
        'type': 'anyone',
    }
).execute()
    drive_link = uploaded.get("webViewLink")


    # OCR
    image = Image.open(filepath)
    raw_text = pytesseract.image_to_string(image)
    cleaned_text = "\n".join([line.strip() for line in raw_text.splitlines() if line.strip()])

    # GPT Prompt
    prompt = f"""
You are an AI assistant that extracts structured information from scanned receipt text.

Receipt text:
\"\"\"
{cleaned_text}
\"\"\"

Extract the following in valid JSON format ONLY:
{{
    "vendor": "Vendor Name",
    "date": "YYYY-MM-DD",
    "total_amount": "$0.00",
    
}}
"""

    try:
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You extract structured data from receipts."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        structured_data = response.choices[0].message.content.strip()
        print("\n=== GPT OUTPUT ===\n", structured_data)

        # Extract JSON from GPT response
        match = re.search(r"\{.*\}", structured_data, re.DOTALL)
        if not match:
            return jsonify({"error": "Could not extract JSON from GPT"}), 500

        data = json.loads(match.group())

        # Append to Google Sheet
        scope = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=scope)
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME).sheet1

        # Add header row if missing
        existing = sheet.get_all_values()
        if not existing or existing[0] != HEADERS:
            sheet.insert_row(HEADERS, index=1)

        # Prepare row
        row = [
        data.get("vendor", ""),
        data.get("date", ""),
        data.get("total_amount", ""),
        ", ".join(data.get("items", [])),
        drive_link  # <-- Adds link to image in Google Drive
    ]

        sheet.append_row(row)

        return jsonify({
            "message": "Receipt processed and saved to Google Sheets.",
            "data": data
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def home():
    return "✅ Receipt Scanner Flask API is running."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

