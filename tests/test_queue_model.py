from app.ui import QueueJob


def test_queue_job_running_restores_as_pending():
    job = QueueJob.from_dict({
        "inputs": ["a.mp4"],
        "mode": "rounds",
        "rounds": 10,
        "hours": 10,
        "minutes": 0,
        "output": "out.mp4",
        "status": "진행 중",
    })
    assert job.status == "대기"
