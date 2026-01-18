import streamlit as st
import os
import glob
import pandas as pd
from datetime import datetime
from pypdf import PdfReader
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# --- 1. 系統設定 ---
st.set_page_config(page_title="創傷知情 AI 家教 (閱讀組-最終版)", layout="wide")

if "history" not in st.session_state: st.session_state.history = []
if "user_nickname" not in st.session_state: st.session_state.user_nickname = ""
if "chat_session" not in st.session_state: st.session_state.chat_session = None

# --- 2. 教材讀取邏輯 ---
@st.cache_resource
def load_pdfs():
    combined_text = ""
    # 搜尋當前目錄所有 PDF
    pdf_files = glob.glob("*.pdf") + glob.glob("*.PDF")
    if not pdf_files:
        return None, []
    try:
        for filename in pdf_files:
            reader = PdfReader(filename)
            for page in reader.pages:
                text = page.extract_text()
                if text: combined_text += text + "\n"
        return combined_text, pdf_files
    except Exception as e:
        return f"Error: {e}", []

cached_text, found_files = load_pdfs()

# --- 3. 登入區 ---
if not st.session_state.user_nickname:
    st.title("📚 創傷知情 AI 家教 (閱讀組)")
    st.info("老師您好，我是您的 AI 家教。請先輸入暱稱以開始。")
    nickname_input = st.text_input("暱稱：", placeholder="例如：兆祺心理師...")
    if st.button("🚀 開始學習"):
        if nickname_input.strip():
            st.session_state.user_nickname = nickname_input
            st.rerun()
    st.stop()

# --- 4. 側邊欄 (功能完整版) ---
st.sidebar.title(f"👤 學員: {st.session_state.user_nickname}")
st.sidebar.markdown("---")

api_key = st.sidebar.text_input("🔑 API Key", type="password")
valid_model = None
if api_key:
    try:
        genai.configure(api_key=api_key)
        available = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        if available:
            valid_model = st.sidebar.selectbox("🤖 AI 模型", available)
    except:
        st.sidebar.error("❌ API Key 無效")

lang = st.sidebar.selectbox("🌐 語言", ["繁體中文", "粵語", "English"])

if not found_files:
    st.sidebar.error("⚠️ 偵測不到 PDF")
else:
    st.sidebar.success(f"✅ 教材已載入：{', '.join(found_files)}")

# 下載按鈕 (對話後出現)
if st.session_state.history:
    st.sidebar.markdown("---")
    df = pd.DataFrame(st.session_state.history)
    df['nickname'] = st.session_state.user_nickname
    csv = df.to_csv(index=False).encode('utf-8-sig')
    st.sidebar.download_button("📥 下載學習紀錄 (CSV)", data=csv, file_name="學習筆記.csv", mime="text/csv")

# --- 5. 對話區 ---
st.title("📖 創傷知情概念導讀區")

for msg in st.session_state.history:
    with st.chat_message("assistant" if msg["role"] == "assistant" else "user"):
        st.write(msg["content"])

user_in = st.chat_input("詢問概念（例如：什麼是 4F 反應？）...")

if user_in:
    if not api_key:
        st.error("❌ 請輸入 API Key")
    elif not cached_text:
        st.error("❌ 找不到教材內容")
    else:
        st.session_state.history.append({"role": "user", "content": user_in})
        try:
            if st.session_state.chat_session is None:
                model = genai.GenerativeModel(
                    model_name=valid_model if valid_model else "gemini-1.5-flash",
                    safety_settings={HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE}
                )
                prompt = f"Role: TIC Tutor. Context: {cached_text[:30000]}. Language: {lang}. Style: Socratic."
                st.session_state.chat_session = model.start_chat(history=[
                    {"role": "user", "parts": [prompt]},
                    {"role": "model", "parts": ["Ready."]}
                ])
            resp = st.session_state.chat_session.send_message(user_in)
            st.session_state.history.append({"role": "assistant", "content": resp.text})
            st.rerun()
        except Exception as e:
            st.error(f"❌ AI 回應失敗：{e}")

if not st.session_state.history:
    with st.chat_message("assistant"):
        st.write(f"你好 {st.session_state.user_nickname} 老師！我是 AI 家教。今天想了解什麼呢？")
