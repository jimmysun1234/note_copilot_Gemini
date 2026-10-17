import os
import re
import json
from pydantic import BaseModel, Field
from typing import Optional
from google import genai
from google.genai import types
from github import Github, Auth

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_NAME = os.getenv("GITHUB_REPOSITORY")
ISSUE_NUMBER = int(os.getenv("ISSUE_NUMBER", "0"))

client = genai.Client(api_key=GEMINI_API_KEY)
auth = Auth.Token(GITHUB_TOKEN)
gh = Github(auth=auth)
repo = gh.get_repo(REPO_NAME)

class UpdateOutput(BaseModel):
    updated_summary: str = Field(description="更新後的 3~5 個重點核心精華摘要 (Markdown 格式)")
    updated_notes: str = Field(description="整合使用者補充回答後的完整結構化筆記 (Markdown 格式)")
    next_question: Optional[str] = Field(default=None, description="如果還有其他尚未釐清的模糊或缺漏處，請再提出『一個』問題。如果所有問題都已釐清，請填 null。")

def process_comment():
    issue = repo.get_issue(number=ISSUE_NUMBER)
    
    # 從 Issue 內容找出對應的筆記檔案路徑與 PDF 名稱
    match = re.search(r"`(notes/[^`]+\.md)`", issue.body)
    if not match:
        print("此 Issue 非筆記補全任務，略過。")
        return
    
    note_path = match.group(1)
    base_name = os.path.splitext(os.path.basename(note_path))[0]
    pdf_path = f"incoming/{base_name}.pdf"

    if not os.path.exists(note_path):
        print(f"找不到筆記檔案: {note_path}")
        return

    with open(note_path, "r", encoding="utf-8") as f:
        current_note = f.read()

    # 收集 Issue 中的所有對話紀錄
    conversation = f"初始提問：\n{issue.body}\n\n"
    for comment in issue.get_comments():
        conversation += f"[{comment.user.login} 回覆]：\n{comment.body}\n\n"

    contents = [
        f"目前的 Markdown 筆記內容：\n{current_note}\n\n",
        f"問答釐清紀錄：\n{conversation}\n\n",
        "請根據使用者的最新回覆，修正並補全筆記內容與摘要。若原板書還有其他未釐清的疑問，請在 next_question 提出下一個問題（一次一題）；若已全部清楚，next_question 請填 null。"
    ]

    # 如果原 PDF 還在，一併附上讓 AI 對照
    if os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        contents.insert(0, types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))

    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction="你是一位專業的學習助理。請將使用者的回答無縫整合進原有的課堂筆記中，修正錯誤或補齊缺漏的推導。數學與電路公式請維持標準 LaTeX 格式。",
            response_mime_type="application/json",
            response_schema=UpdateOutput,
        )
    )

    if response.parsed:
        res = response.parsed
        summary, notes, next_q = res.updated_summary, res.updated_notes, res.next_question
    else:
        data = json.loads(response.text)
        summary = data.get("updated_summary", "")
        notes = data.get("updated_notes", "")
        next_q = data.get("next_question")

    prompt_block = (
        "```markdown\n"
        f"你現在是該領域的教學專家。請根據上述「{base_name}」的筆記內容，出 3 題觀念驗證題（包含 1 題陷阱選擇題與 2 題推導/問答題），並附上詳細詳解與評分標準。\n"
        "```"
    )

    final_content = (
        f"# 課堂筆記：{base_name}\n\n"
        f"## 📌 3 分鐘核心摘要\n{summary}\n\n"
        f"---\n\n"
        f"## 📖 結構化重點筆記\n{notes}\n\n"
        f"---\n\n"
        f"## 💡 後續複習提示詞 (Prompt)\n{prompt_block}\n"
    )

    with open(note_path, "w", encoding="utf-8") as f:
        f.write(final_content)
    print(f"✅ 已根據回覆更新筆記：{note_path}")

    # 判斷是否還有下一題，或是結案
    if next_q and str(next_q).strip().lower() != "null":
        issue.create_comment(
            f"✅ 已將您的回覆整合至 `{note_path}`！\n\n"
            f"### 追問下一題：\n> **{next_q}**\n\n"
            f"請直接於下方繼續留言回覆。"
        )
    else:
        issue.create_comment(
            f"🎉 感謝確認！所有疑問皆已釐清，最終完整版筆記已更新至 `{note_path}`。\n\n"
            f"---\n### 💡 專屬後續複習提示詞\n{prompt_block}"
        )
        issue.edit(state="closed")

if __name__ == "__main__":
    process_comment()
