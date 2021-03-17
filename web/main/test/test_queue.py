from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta


from django.conf import settings
from django.db import connections
from django.utils import timezone
from rest_framework.settings import api_settings

from ..models import CaptureJob
from ..tasks import clean_up_failed_captures

import pytest


def test_job_queue_order(user_factory, pending_capture_job_factory):
    """
    Jobs should be processed round-robin, one per user.
    """

    user_one = user_factory()
    user_two = user_factory()

    jobs = [
        pending_capture_job_factory(user=user_one, human=True),
        pending_capture_job_factory(user=user_one, human=True),
        pending_capture_job_factory(user=user_one, human=True),
        pending_capture_job_factory(user=user_two, human=True),

        pending_capture_job_factory(user=user_two),

        pending_capture_job_factory(user=user_one, human=True),
        pending_capture_job_factory(user=user_one, human=True),
        pending_capture_job_factory(user=user_one, human=True),
        pending_capture_job_factory(user=user_two, human=True),
    ]

    expected_order = [
        0, 3,  # u1, u2
        1, 8,  # u1, u2
        2, 5, 6, 7,  # remaining u1 jobs
        4  # robots queue
    ]

    # test CaptureJob.queue_position
    for i, job in enumerate(jobs):
        queue_position = job.queue_position()
        expected_queue_position = expected_order.index(i)+1
        assert queue_position == expected_queue_position, f"Job {i} has queue position {queue_position}, should be {expected_queue_position}."

    # test CaptureJob.get_next_job
    expected_next_jobs = [jobs[i] for i in expected_order]
    next_jobs = [CaptureJob.get_next_job(reserve=True) for i in range(len(jobs))]
    assert next_jobs == expected_next_jobs


@pytest.mark.django_db(transaction=True)
def test_race_condition_prevented(pending_capture_job_factory):
    """
    Fetch two jobs at the same time in threads and make sure same job isn't returned to both.
    """
    jobs = [
        pending_capture_job_factory(),
        pending_capture_job_factory()
    ]

    def get_next_job(i):
        job = CaptureJob.get_next_job(reserve=True)
        for connection in connections.all():
            connection.close()
        return job

    CaptureJob.TEST_PAUSE_TIME = .1
    with ThreadPoolExecutor(max_workers=2) as e:
        fetched_jobs = e.map(get_next_job, range(2))
    CaptureJob.TEST_PAUSE_TIME = 0

    assert set(jobs) == set(fetched_jobs)


@pytest.mark.django_db(transaction=True)
def test_race_condition_not_prevented(pending_capture_job_factory):
    """
    Make sure that test_race_condition_prevented is passing for the right reason --
    should fail if race condition protection is disabled.
    """
    CaptureJob.TEST_ALLOW_RACE = True
    with pytest.raises(AssertionError, match="Extra items in the left set"):
        test_race_condition_prevented(pending_capture_job_factory)
    CaptureJob.TEST_ALLOW_RACE = False


def test_hard_timeout(pending_capture_job):

    # simulate a failed run_next_capture()
    job = CaptureJob.get_next_job(reserve=True)

    # capture_start_time should be set accurately on the server side
    assert (job.capture_start_time - timezone.now()).total_seconds() < 60

    # clean_up_failed_captures shouldn't affect job, since timeout hasn't passed
    clean_up_failed_captures()
    job.refresh_from_db()
    assert job.status == CaptureJob.Status.IN_PROGRESS

    # once job is sufficiently old, clean_up_failed_captures should mark it as failed
    job.capture_start_time -= timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT+60)
    job.save()
    clean_up_failed_captures()
    job.refresh_from_db()
    assert job.status == CaptureJob.Status.FAILED

    # failed jobs will have a message indicating failure reason
    assert job.message[api_settings.NON_FIELD_ERRORS_KEY][0] == "Timed out."
