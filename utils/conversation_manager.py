"""
Conversation History Manager
Maintains conversation context per chat
"""

import logging
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

class ConversationManager:
    """Manage conversation history for each chat"""
    
    MAX_HISTORY = 20  # Keep last 20 messages
    
    def __init__(self):
        """Initialize conversation manager"""
        # {chat_id: deque of messages}
        self.conversations = defaultdict(lambda: deque(maxlen=self.MAX_HISTORY))
        logger.info("Conversation Manager initialized")
    
    def add_message(self, chat_id: int, role: str, content: str):
        """Add message to conversation history"""
        message = {
            "role": role,  # 'user' or 'assistant'
            "content": content
        }
        self.conversations[chat_id].append(message)
        logger.debug(f"Added {role} message to chat {chat_id}")
    
    def get_history(self, chat_id: int) -> list:
        """Get conversation history for a chat"""
        return list(self.conversations[chat_id])
    
    def clear_history(self, chat_id: int):
        """Clear conversation history for a chat"""
        self.conversations[chat_id].clear()
        logger.info(f"Cleared history for chat {chat_id}")
    
    def get_message_count(self, chat_id: int) -> int:
        """Get number of messages in history"""
        return len(self.conversations[chat_id])
