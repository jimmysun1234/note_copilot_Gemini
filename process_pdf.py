import os
import glob
import json
from google import genai
from google.genai import types
from github import Github

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

client = genai.Client(api_key=GEMINI_API_KEY)
gh = Github(GITHUB_TOKEN)
repo = gh.get_repo(REPO_NAME)

SYSTEM_PROMPT = """
你是一位專業的高效學習助理與知識工程師。你的任務是閱讀使用者上傳的課堂板書/筆記 PDF，將其整理為條理分明、概念嚴謹的 Markdown 筆記。

請嚴格以 JSON 格式回應，格式如下：
{
  "summary": "3~5 個重點的核心精華摘要 (Markdown 格式)",
  "notes": "完整的結構化筆記，包含章節、公式推導、關鍵觀念、範例 (Markdown 格式)",
  "question": "如果板書有缺漏、遮蔽、筆跡模糊或邏輯中斷，請提出『一個』最關鍵的具體問題向使用者確認。如果內容非常完整無缺漏，此欄位請填 null。"
}
"""

def process_latest_pdf():
    pdf_files = glob.glob("incoming/*.pdf")
    if not pdf_files:
        print("未在 incoming/ 找到任何 PDF 檔案。")
        return

    # 取得最新上傳的 PDF 檔案
    latest_pdf = max(pdf_files, key=os.path.getctime)
    pdf_name = os.path.basename(latest_pdf)
    base_name = os.path.splitext(pdf_name)[0]
    output_md_path = f"notes/{base_name}.md"

    print(f"正在處理檔案: {latest_pdf}")

    # 上傳 PDF 至 Gemini
    uploaded_file = client.files.upload(file=latest_pdf)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            uploaded_file,
            "請閱讀這份板書 PDF，並依照規範產出摘要、筆記結構與缺漏提問。"
        ],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json"
        )
    )

    data = json.loads(response.text)
    summary = data.get("summary", "")
    notes = data.get("notes", "")
    question = data.get("question")

    final_content = f"""# 課堂筆記：{base_name}

## 📌 3 分鐘核心摘要
{summary}

---

## 📖 結構化重點筆記
{notes}

---

## 💡 後續複習提示詞 (Prompt)
```markdown
你現在是該領域的教學專家。請根據上述「{base_name}」的筆記內容，出 3 題觀念驗證題（包含 1 題陷阱選擇題與 2 題推導/問答題），並附上詳細詳解與評分標準。
