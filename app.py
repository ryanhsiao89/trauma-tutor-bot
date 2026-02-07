import streamlit as st
import os
import glob
import pandas as pd
from datetime import datetime, timedelta
from pypdf import PdfReader
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import time

# --- 1. 系統設定 ---
st.set_page_config(page_title="創傷知情 AI 家教 (研究版)", layout="wide")

# --- 0. 檢查是否剛登出 (放在最前面攔截) ---
if st.session_state.get("logout_triggered"):
    st.markdown("## ✅ 已成功登出")
    st.success("您的學習紀錄已安全上傳至雲端。感謝您的參與！")
    st.write("如果您需要再次學習，請點擊下方按鈕。")
    
    if st.button("🔄 重新登入"):
        st.session_state.logout_triggered = False
        st.rerun()
    st.stop()

# --- Google Sheets 上傳函式 (Tutor 專用版) ---
def save_to_google_sheets(user_id, chat_history, lang):
    try:
        # 1. 檢查 Secrets 是否存在
        if "gcp_service_account" not in st.secrets:
            st.error("❌ 錯誤：找不到 Google Cloud 金鑰 (Secrets)。")
            return False

        # 2. 連線設定 (包含金鑰格式修復)
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        
        # 3. 開啟試算表 (檔名必須完全一致)
        target_sheet_name = "2025創傷知情研習數據" 
        try:
            sheet = client.open(target_sheet_name)
        except gspread.SpreadsheetNotFound:
            st.error(f"❌ 錯誤：找不到名為「{target_sheet_name}」的試算表。請確認 Google Drive 上的檔名完全一致。")
            return False

        # 4. 取得或自動建立 'Tutor' 分頁
        try:
            worksheet = sheet.worksheet("Tutor")
        except gspread.WorksheetNotFound:
            worksheet = sheet.add_worksheet(title="Tutor", rows="1000", cols="10")
            worksheet.append_row(["登入時間", "登出時間", "學員編號", "使用分鐘數", "累積使用次數", "完整對話紀錄"])
            st.toast("💡 系統已自動為您建立 'Tutor' 分頁")
        
        # 5. 時間計算 (校正為台灣時間 UTC+8)
        tw_fix = timedelta(hours=8)
        start_t = st.session_state.get('start_time', datetime.now())
        login_str = (start_t + tw_fix).strftime("%Y-%m-%d %H:%M:%S")
        end_t = datetime.now()
        logout_str = (end_t + tw_fix).strftime("%Y-%m-%d %H:%M:%S")
        duration_mins = round((end_t - start_t).total_seconds() / 60, 2)
        
        # 6. 計算累積次數
        try:
            all_ids = worksheet.col_values(3) 
            login_count = all_ids.count(user_id) + 1
        except:
            login_count = 1

        # 7. 整理對話內容
        context_info = f"使用語言: {lang}"
        full_conversation = f"【設定參數】：{context_info}\n\n"
        for msg in chat_history:
            role = msg.get("role", "Unknown")
            content = ""
            if "parts" in msg:
                content = msg["parts"][0] if isinstance(msg["parts"], list) else str(msg["parts"])
            elif "content" in msg:
                content = msg["content"]
            full_conversation += f"[{role}]: {content}\n"

        # 8. 寫入資料
        worksheet.append_row([
            login_str, 
            logout_str, 
            user_id, 
            duration_mins, 
            login_count, 
            full_conversation
        ])
        return True

    except Exception as e:
        st.error(f"❌ 上傳發生錯誤: {str(e)}") 
        return False

# --- 自動重試機制函式 ---
def send_message_with_retry(chat_session, text, retries=3, delay=2):
    """
    發送訊息給 Gemini，若失敗則自動重試。
    """
    for attempt in range(retries):
        try:
            response = chat_session.send_message(text)
            return response.text
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay)  # 等待後重試
            else:
                raise e  # 超過重試次數則拋出錯誤

# --- 格式化下載內容函式 (新增) ---
def convert_history_to_txt(history):
    text_content = ""
    for msg in history:
        role_name = "AI 家教" if msg["role"] == "assistant" else "學員"
        content = msg["content"]
        text_content += f"【{role_name}】：\n{content}\n\n{'='*20}\n\n"
    return text_content

# 初始化 Session State
if "history" not in st.session_state: st.session_state.history = []
if "loaded_text" not in st.session_state: st.session_state.loaded_text = ""
if "user_nickname" not in st.session_state: st.session_state.user_nickname = ""
if "start_time" not in st.session_state: st.session_state.start_time = datetime.now()

# --- 2. 登入區 (編號制) ---
if not st.session_state.user_nickname:
    st.title("📚 創傷知情 AI 家教 (Tutor)")
    st.info("請輸入您的研究編號 (ID) 以開始學習。")
    
    nickname_input = st.text_input("請輸入您的編號：", placeholder="例如：001, 002...") 
    
    if st.button("🚀 進入教室"):
        if nickname_input.strip():
            st.session_state.user_nickname = nickname_input
            st.session_state.start_time = datetime.now()
            st.rerun()
        else:
            st.error("❌ 編號不能為空！")
    st.stop()

# --- 3. 側邊欄設定 ---
st.sidebar.title(f"👤 學員: {st.session_state.user_nickname}")

# [新增功能 1] 下載對話紀錄
st.sidebar.markdown("---")
st.sidebar.markdown("### 📥 下載紀錄")
if st.session_state.history:
    chat_txt = convert_history_to_txt(st.session_state.history)
    st.sidebar.download_button(
        label="下載對話紀錄 (.txt)",
        data=chat_txt,
        file_name=f"Tutor_History_{st.session_state.user_nickname}.txt",
        mime="text/plain"
    )

st.sidebar.markdown("---")
st.sidebar.markdown("### 📤 結束學習")

if st.sidebar.button("上傳紀錄並登出"):
    if not st.session_state.history:
        st.sidebar.warning("還沒有對話紀錄喔！")
    else:
        with st.spinner("正在連線至 Google 試算表..."):
            current_lang = st.session_state.get("current_lang", "未設定")
            
            upload_success = save_to_google_sheets(st.session_state.user_nickname, st.session_state.history, current_lang)
            
            if upload_success:
                st.sidebar.success("✅ 上傳成功！")
                time.sleep(1) 
                keys_to_clear = ["user_nickname", "history", "start_time", "chat_session"]
                for key in keys_to_clear:
                    if key in st.session_state:
                        del st.session_state[key]
                st.session_state.logout_triggered = True
                st.rerun()
            else:
                st.sidebar.error("⚠️ 上傳失敗，請檢查上方錯誤訊息。")
                if st.sidebar.button("⚠️ 忽略錯誤，強制登出"):
                    st.session_state.logout_triggered = True
                    st.session_state.clear()
                    st.rerun()

# API Key 與設定
st.sidebar.markdown("---")
st.sidebar.warning("🔑 請輸入您自己的 Gemini API Key")
api_key = st.sidebar.text_input("在此貼上您的 API Key", type="password")

if not api_key:
    st.info("💡 提示：請先在側邊欄輸入 API Key，否則系統無法運作。")
    st.stop() 

# 自動偵測模型
valid_model_name = None
if api_key:
    try:
        genai.configure(api_key=api_key)
        available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        if available_models:
            valid_model_name = st.sidebar.selectbox("🤖 AI 模型", available_models)
    except: 
        st.sidebar.error("❌ API Key 無效")

# 選項設定 (Tutor 主要是語言選項)
lang = st.sidebar.selectbox("🌐 選擇對話語言", ["繁體中文", "粵語", "English"])
st.session_state.current_lang = lang

# --- 4. 自動讀取教材 ---
if not st.session_state.loaded_text:
    combined_text = ""
    pdf_files = glob.glob("*.pdf") + glob.glob("*.PDF") # 支援大小寫
    if pdf_files:
        with st.spinner(f"📚 正在內化 {len(pdf_files)} 份教材..."):
            try:
                for filename in pdf_files:
                    reader = PdfReader(filename)
                    for page in reader.pages:
                        text = page.extract_text()
                        if text: combined_text += text + "\n"
                st.session_state.loaded_text = combined_text
                st.toast(f"✅ 已載入 {len(pdf_files)} 份教材")
            except Exception as e:
                st.error(f"教材讀取失敗: {e}")
    else:
        st.warning("⚠️ 倉庫中找不到 PDF 檔案。")

# --- 5. 家教對話邏輯 (Mollick Tutor Prompt) ---
st.title("📖 創傷知情概念導讀區")

if st.session_state.loaded_text and api_key and valid_model_name:
    model = genai.GenerativeModel(
        model_name=valid_model_name,
        safety_settings={
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        }
    )

    if len(st.session_state.history) == 0:
        # [改良部分 2] 核心 Prompt：加入針對真實個案提問的拒絕機制
        sys_prompt = f"""
        Role: You are a "Trauma-Informed Care Tutor" (Mollick's Tutor Persona).
        Target Audience: A teacher learning about Trauma-Informed Care (TIC).
        Language: {lang}.
        
        Knowledge Base (Context): {st.session_state.loaded_text[:30000]}
        
        ### METHODOLOGY (Mollick's Tutor Model):
        1. **Assess & Explain:** When the user asks a question, explain the concept clearly and directly based on the Knowledge Base.
        2. **Provide Examples:** Always give a concrete, classroom-based example to illustrate the concept.
        3. **Check for Understanding (CRITICAL):** After explaining, *ALWAYS* ask the user a question to verify they understood.
           - Example Check: "Does this make sense to you?"
           - Example Check: "How might you see this appearing in your classroom?"
           - Example Check: "Could you try explaining the 'Flight' response back to me in your own words?"
        
        ### STRICT BOUNDARIES & RULES:
        1. **Scope Restriction:** You are an AI Tutor for *learning concepts*, NOT a supervisor for clinical cases.
        2. **Refusal Logic:** If the user asks for advice on specific, real-world student cases, personal counseling issues, or practical intervention strategies for specific students (e.g., "I have a student who does X, what should I do?"), you MUST politely decline.
        3. **Refusal Script:** "我是協助您學習創傷知情概念的 AI 家教，無法針對真實個案提供諮商建議或處遇策略。請我們回到教材內容，探討相關的理論概念好嗎？" (Translate this sentiment to the user's language if needed).
        4. **Redirect:** After declining, explicitly ask them to pose a question about a concept from the reading material instead.
        5. **Teaching Mode:** Do NOT just be a passive search engine. Be an *active teacher*.
        6. **Correction:** If the user's answer is wrong, correct them gently and re-explain.
        
        Start the conversation by introducing yourself as their TIC Tutor and asking what concept they would like to learn about today (e.g., 4F responses, window of tolerance, etc.).
        """
        
        welcome_msg = f"你好 {st.session_state.user_nickname} 老師！我是您的創傷知情 AI 家教。\n\n我的工作是協助您弄懂那些複雜的理論，並確認您能運用在教學上。今天您想了解哪個概念？（例如：什麼是 4F 反應？什麼是耐受窗？）"
        
        st.session_state.chat_session = model.start_chat(history=[
            {"role": "user", "parts": [sys_prompt]},
            {"role": "model", "parts": [welcome_msg]}
        ])
        st.session_state.history.append({"role": "assistant", "content": welcome_msg})

    for msg in st.session_state.history:
        role = "assistant" if msg["role"] == "assistant" else "user"
        with st.chat_message(role):
            st.write(msg["content"])

    if user_in := st.chat_input("詢問概念..."):
        st.session_state.history.append({"role": "user", "content": user_in})
        with st.chat_message("user"):
            st.write(user_in)
            
        with st.spinner("👩‍🏫 家教思考中..."):
            try:
                # 使用改良後的重試機制發送訊息
                resp_text = send_message_with_retry(st.session_state.chat_session, user_in)
                st.session_state.history.append({"role": "assistant", "content": resp_text})
                st.rerun()
            except Exception as e:
                st.error(f"❌ 發生錯誤 (已重試 3 次): {e}")
