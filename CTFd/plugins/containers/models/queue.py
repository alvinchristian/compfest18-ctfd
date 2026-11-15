from datetime import datetime
import uuid
from CTFd.models import db


class ContainerQueueRequest(db.Model):
    __tablename__ = "container_queue_requests"
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    challenge_id = db.Column(db.Integer, nullable=False, index=True)
    account_id = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="queued", index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
