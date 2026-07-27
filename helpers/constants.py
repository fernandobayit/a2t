PLUGIN_NAME = "a2t"
DOWNLOAD_FOLDER = "usr/uploads"
STATE_FILE = "usr/plugins/a2t/state.json"

# Context data keys
CTX_TG_BOT = "a2t_bot"
CTX_TG_BOT_CFG = "a2t_bot_cfg"
CTX_TG_CHAT_ID = "a2t_chat_id"
CTX_TG_USER_ID = "a2t_user_id"
CTX_TG_USERNAME = "a2t_username"
CTX_TG_TYPING_STOP = "_a2t_typing_stop"
CTX_TG_REPLY_TO = "_a2t_reply_to_message_id"

# Transient (used between tool_execute_after and process_chain_end)
CTX_TG_ATTACHMENTS = "_a2t_response_attachments"
CTX_TG_KEYBOARD = "_a2t_response_keyboard"

# A2T-specific context data keys
CTX_TG_PROJECT = "a2t_project"

CTX_TG_THREAD_ID = "a2t_thread_id"
CTX_TG_API_THREAD_ID = "a2t_api_thread_id"
