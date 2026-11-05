"""Transaction-bound saver for this sequential graph; commits are owned by the worker."""
from threading import RLock

from langgraph.checkpoint.base import (BaseCheckpointSaver, CheckpointTuple,
                                      WRITES_IDX_MAP, get_checkpoint_metadata)
from sqlalchemy import select

from .db import GraphCheckpointRow, GraphWriteRow


class DatabaseSaver(BaseCheckpointSaver):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.lock = RLock()

    def get_tuple(self, config):
        with self.lock:
            value = config["configurable"]
            thread, ns = value["thread_id"], value.get("checkpoint_ns", "")
            query = select(GraphCheckpointRow).where(GraphCheckpointRow.thread_id == thread,
                                                     GraphCheckpointRow.namespace == ns)
            if value.get("checkpoint_id"):
                query = query.where(GraphCheckpointRow.checkpoint_id == value["checkpoint_id"])
            row = self.db.scalar(query.order_by(GraphCheckpointRow.checkpoint_id.desc()).limit(1))
            if row is None:
                return None
            writes = self.db.scalars(select(GraphWriteRow).where(GraphWriteRow.thread_id == thread,
                GraphWriteRow.namespace == ns, GraphWriteRow.checkpoint_id == row.checkpoint_id)
                .order_by(GraphWriteRow.task_id, GraphWriteRow.index)).all()
            return CheckpointTuple(config={"configurable": {"thread_id": thread, "checkpoint_ns": ns,
                "checkpoint_id": row.checkpoint_id}},
                checkpoint=self.serde.loads_typed((row.data_type, row.data)),
                metadata=self.serde.loads_typed((row.meta_type, row.meta)),
                parent_config={"configurable": {"thread_id": thread, "checkpoint_ns": ns,
                    "checkpoint_id": row.parent_id}} if row.parent_id else None,
                pending_writes=[(w.task_id, w.channel, self.serde.loads_typed((w.data_type, w.data))) for w in writes])

    def put(self, config, checkpoint, metadata, new_versions):
        with self.lock:
            c = config["configurable"]
            kind, data = self.serde.dumps_typed(checkpoint)
            mkind, meta = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
            self.db.merge(GraphCheckpointRow(thread_id=c["thread_id"], namespace=c.get("checkpoint_ns", ""),
                checkpoint_id=checkpoint["id"], parent_id=c.get("checkpoint_id"), data_type=kind,
                data=data, meta_type=mkind, meta=meta))
            self.db.flush()
            return {"configurable": {"thread_id": c["thread_id"], "checkpoint_ns": c.get("checkpoint_ns", ""),
                                     "checkpoint_id": checkpoint["id"]}}

    def put_writes(self, config, writes, task_id, task_path=""):
        with self.lock:
            c = config["configurable"]
            for position, (channel, value) in enumerate(writes):
                index = WRITES_IDX_MAP.get(channel, position)
                key = (c["thread_id"], c.get("checkpoint_ns", ""), c["checkpoint_id"], task_id, index)
                existing = self.db.get(GraphWriteRow, key)
                if existing and index >= 0:
                    continue
                kind, data = self.serde.dumps_typed(value)
                self.db.merge(GraphWriteRow(thread_id=key[0], namespace=key[1], checkpoint_id=key[2],
                    task_id=task_id, index=index, channel=channel, data_type=kind, data=data))
            self.db.flush()

    def list(self, config, *, filter=None, before=None, limit=None):
        with self.lock:
            query = select(GraphCheckpointRow)
            if config:
                c = config["configurable"]
                query = query.where(GraphCheckpointRow.thread_id == c["thread_id"],
                                     GraphCheckpointRow.namespace == c.get("checkpoint_ns", ""))
            if before:
                query = query.where(GraphCheckpointRow.checkpoint_id < before["configurable"]["checkpoint_id"])
            rows = self.db.scalars(query.order_by(GraphCheckpointRow.checkpoint_id.desc())).all()
            emitted = 0
            for row in rows:
                result = self.get_tuple({"configurable": {"thread_id": row.thread_id,
                    "checkpoint_ns": row.namespace, "checkpoint_id": row.checkpoint_id}})
                if filter and any(result.metadata.get(k) != v for k, v in filter.items()):
                    continue
                if limit is not None and emitted >= limit:
                    break
                emitted += 1
                yield result
