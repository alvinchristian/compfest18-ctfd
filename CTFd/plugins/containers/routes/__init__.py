"""
Container Plugin Routes
"""

from .admin import admin_bp
from .user import user_bp

__all__ = ["user_bp", "admin_bp"]
