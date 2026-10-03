"""VetanKosh low-resource durable worker.
Run exactly one instance on Oracle Free Tier: python worker.py
Scale by increasing WORKER_COUNT only after adding CPU/RAM.
"""
import os, time, socket, traceback
from datetime import datetime, timedelta
from sqlalchemy import or_
from sqlalchemy.exc import OperationalError
from models import SessionLocal, DurableJob, Form16Generation
from main import send_form16_email_for_generation

POLL=float(os.getenv("JOB_POLL_SECONDS","2"))
LEASE=int(os.getenv("JOB_LEASE_SECONDS","900"))
WORKER_ID=f"{socket.gethostname()}:{os.getpid()}"

def claim_one(db):
    now=datetime.utcnow(); stale=now-timedelta(seconds=LEASE)
    # Recover a worker killed during PDF rendering after its lease expires.
    db.query(DurableJob).filter(DurableJob.status=="processing", DurableJob.locked_at < stale).update({"status":"retry","available_at":now,"locked_at":None,"locked_by":None}, synchronize_session=False)
    db.commit()
    q=(db.query(DurableJob).filter(DurableJob.status.in_(["queued","retry"]), DurableJob.available_at<=now).order_by(DurableJob.created_at.asc()))
    if db.bind.dialect.name == "postgresql": q=q.with_for_update(skip_locked=True)
    job=q.first()
    if not job: return None
    job.status="processing"; job.locked_at=now; job.locked_by=WORKER_ID; job.attempt_count=int(job.attempt_count or 0)+1; db.commit(); return job.id

def run(job_id):
    db=SessionLocal()
    try:
        job=db.query(DurableJob).filter(DurableJob.id==job_id).first()
        if not job: return
        if job.job_type=="form16_generate_email":
            gid=(job.payload_json or {}).get("generation_id")
            et=(job.payload_json or {}).get("email_type","payment_confirmed_form16")
            send_form16_email_for_generation(gid, et)
            db.expire_all(); g=db.query(Form16Generation).filter(Form16Generation.id==gid).first()
            if not g or g.status!="generated": raise RuntimeError("Form 16 generation did not complete")
        else: raise RuntimeError(f"Unknown job type: {job.job_type}")
        job=db.query(DurableJob).filter(DurableJob.id==job_id).first(); job.status="done"; job.completed_at=datetime.utcnow(); job.locked_at=None; job.locked_by=None; job.last_error=None; db.commit()
    except Exception as exc:
        db.rollback(); job=db.query(DurableJob).filter(DurableJob.id==job_id).first()
        if job:
            delay=min(1800, 30*(2**max(0,int(job.attempt_count or 1)-1)))
            job.status="failed" if int(job.attempt_count or 0)>=int(job.max_attempts or 5) else "retry"
            job.available_at=datetime.utcnow()+timedelta(seconds=delay); job.locked_at=None; job.locked_by=None; job.last_error=str(exc)[:2000]; db.commit()
        traceback.print_exc()
    finally: db.close()

def main():
    while True:
        db=SessionLocal()
        try: jid=claim_one(db)
        except OperationalError: db.rollback(); jid=None
        finally: db.close()
        if jid: run(jid)
        else: time.sleep(POLL)
if __name__=="__main__": main()
