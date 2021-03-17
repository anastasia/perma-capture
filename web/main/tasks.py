from celery import shared_task
from celery.exceptions import MaxRetriesExceededError, SoftTimeLimitExceeded, Retry
from celery.signals import task_failure
import docker
import os
import requests
import socket
import threading
from time import sleep
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import mail_admins

from .models import CaptureJob, Archive, WebhookSubscription
from .serializers import ReadOnlyCaptureJobSerializer, SimpleWebhookSubscriptionSerializer
from .storages import get_storage
from .utils import (validate_and_clean_url, get_file_hash, parse_querystring,
    datetime_from_timestamp, sign_data, is_valid_signature, send_template_email
)

from pytest import raises as assert_raises

from test.test_helpers import raise_on_call

import logging
logger = logging.getLogger(__name__)


### ERROR REPORTING ###

@task_failure.connect()
def celery_task_failure_email(**kwargs):
    """
    Celery 4.0+ has no method to send emails on failed tasks.
    This event handler provides equivalent functionality to Celery < 4.0,
    and reports truly failed tasks, like those terminated after CELERY_TASK_TIME_LIMIT
    and those that throw uncaught exceptions.
    From https://github.com/celery/celery/issues/3389

    Given:
    >>> _, mocker = [getfixture(i) for i in ['celery_worker', 'mocker']]
    >>> sleep = mocker.patch('main.tasks.sleep')
    >>> mailer = mocker.patch('main.tasks.mail_admins')

    Patch the task so that it errors, and then run it:
    >>> sleep.side_effect = lambda seconds: 1/0
    >>> _ = demo_scheduled_task.apply_async(kwargs={'pause_for_seconds': 1})

    Wait for the task to execute and return (without using the patched copy of sleep)
    >>> import time; time.sleep(1)

    Observe that an error email was sent, as expected.
    >>> mailer.assert_called_once()
    """
    subject = "[{queue_name}@{host}] Error: Task {sender.name} ({task_id}): {exception}".format(
        queue_name='celery',
        host=socket.gethostname(),
        **kwargs
    )

    message = """Task {sender.name} with id {task_id} raised exception:
{exception!r}
Task was called with args: {args} kwargs: {kwargs}.
The contents of the full traceback was:
{einfo}
    """.format(
        **kwargs
    )
    mail_admins(subject, message)


### CAPTURE HELPERS ###

class HaltCaptureException(Exception):
    """
    An exception we can trigger to halt capture and release all involved resources.
    """
    pass


class BrowsertrixLifeCycleThread(threading.Thread):
    """
    """
    def __init__(self, container, timeout, *args, **kwargs):
        self.container = container
        self.timeout = timeout
        self.timedout = False
        self.result = {}
        self.status = None
        self.exit_code = None
        self.stderr = None
        super().__init__(*args, **kwargs)

    def run(self):
        try:
            self.result = self.container.wait(timeout=self.timeout)
        except requests.exceptions.ConnectionError:
            self.timedout = True
            # For now, just kill the container. We likely want something gentler,
            # that requests Browsertrix stop recording and finalize the record,
            # and then a second timeout, so as to preserve partial capture results.
            self.container.stop(timeout=0)
            self.result = self.container.wait(timeout=1)
        finally:
            try:
                # If the container has been forceably shut down by the main thread,
                # these calls will fail.
                self.container.reload()
                self.stderr = str(self.container.logs(stdout=False), 'utf-8')
            except requests.exceptions.RequestException:
                pass
            self.exit_code = self.result.get('StatusCode')
            self.status = self.container.status


def inc_progress(capture_job, inc, description):
    capture_job.inc_progress(inc, description)
    logger.info(f"{capture_job} step {capture_job.step_count}: {capture_job.step_description}")


def clean_up_failed_captures():
    """
    Clean up any existing jobs that are marked in_progress but must have timed out by now, based on our hard timeout
    setting.
    """
    # use database time with a custom where clause to ensure consistent time across workers
    for capture_job in CaptureJob.objects.filter(status=CaptureJob.Status.IN_PROGRESS).extra(
            where=[f"capture_start_time < now() - make_interval(secs => {settings.CELERY_TASK_TIME_LIMIT})"]
    ):
        capture_job.mark_failed("Timed out.")


### TASKS ###

@shared_task
def demo_scheduled_task(pause_for_seconds=0):
    """
    A demo task, scheduled to run once a minute dev. To see it in action,
    set CELERY_TASK_ALWAYS_EAGER = False in setting.py before running `fab-run`.
    """
    if pause_for_seconds:
        sleep(pause_for_seconds)
    return "Celerybeat is working!"


@shared_task
def run_next_capture():
    """
    Given:
    >>> pending_capture_job_factory, docker_client, django_settings, mocker, caplog = [getfixture(i) for i in ['pending_capture_job_factory', 'docker_client', 'settings', 'mocker', 'caplog']]

    Helpers:
    >>> orig_clean_up_failed = clean_up_failed_captures
    >>> mock_clean_up_failed = mocker.patch('main.tasks.clean_up_failed_captures')
    >>> mock_clean_up_failed.side_effect = orig_clean_up_failed

    >>> orig_inc_progress = inc_progress
    >>> mock_inc_progress = mocker.patch('main.tasks.inc_progress')
    >>> def run_test_capture(url, stop_before_step=None, throw=None):
    ...     if stop_before_step:
    ...         mock_inc_progress.side_effect = raise_on_call(orig_inc_progress, stop_before_step + 1, HaltCaptureException)
    ...     else:
    ...         mock_inc_progress.side_effect = orig_inc_progress
    ...     job = pending_capture_job_factory(requested_url=url)
    ...     _ = run_next_capture.apply()
    ...     job.refresh_from_db()
    ...     return job

    NO JOBS

    If there are no pending capture jobs, the task simply returns.
    >>> _ = run_next_capture.apply()
    >>> assert "No jobs" in caplog.text
    >>> caplog.clear()

    VALIDATION

    We perform some simple validation on the submitted URL.
    >>> invalid_urls = [
    ...     '',
    ...     'examplecom',
    ...     'https://www.ntanet.org/some-article.pdf\x01',
    ...     'file:///etc/passwd',
    ... ]
    >>> for url in invalid_urls:
    ...     job = run_test_capture(url)
    ...     assert job.status == CaptureJob.Status.INVALID
    ...     assert job.message['requested_url']
    ...     assert job.step_count == 0
    ...     assert job.step_description == 'Validating.'
    ...     assert job.capture_end_time


    We tidy up submitted URLs: stripping whitespace...
    >>> job = run_test_capture('   http://example.com   ', stop_before_step=1)
    >>> assert job.validated_url == 'http://example.com'

    ...and add http if the protocol is omitted.
    >>> job = run_test_capture('example.com', stop_before_step=1)
    >>> assert job.validated_url == 'http://example.com'

    SUCCESS

    >>> job = run_test_capture('example.com')
    >>> assert job.status == CaptureJob.Status.COMPLETED
    >>> assert job.step_description == 'Saving archive.'
    >>> assert job.capture_end_time
    >>> assert job.archive.warc_size
    >>> assert job.archive.hash and job.archive.hash_algorithm
    >>> assert requests.get(job.archive.download_url).status_code == 200

    FAILURE

    If an exception is thrown in the main thread while Browsertrix is working, we stop and clean up.
    >>> caplog.clear()
    >>> job = run_test_capture('example.com', stop_before_step=6)
    >>> assert job.status == CaptureJob.Status.FAILED
    >>> assert job.step_count == 5
    >>> assert job.step_description == 'Browsertrix: status 2.'
    >>> assert job.capture_end_time
    >>> assert caplog.records[-1].message == 'No jobs waiting!'
    >>> assert not docker_client.containers.list(all=True, filters={'ancestor': settings.BROWSERTRIX_IMAGE})

    If Browsertrix exceeds the maximum permitted time limit, we stop and clean up.
    >>> caplog.clear()
    >>> django_settings.BROWSERTRIX_TIMEOUT_SECONDS = 1
    >>> job = run_test_capture('example.com')
    >>> assert job.status == CaptureJob.Status.FAILED
    >>> assert 'Browsertrix exited with 137: \\nSession terminated, killing shell...' in caplog.text  #  137 means SIGKILL
    >>> assert not docker_client.containers.list(all=True, filters={'ancestor': settings.BROWSERTRIX_IMAGE})

    We clean up failed jobs before we get started.
    >>> assert mock_clean_up_failed.call_count > 0
    """

    # First, clean up failed captures, because their presence might affect the queue order.
    clean_up_failed_captures()

    # Retrieve the next job in the queue
    capture_job = CaptureJob.get_next_job(reserve=True)
    if not capture_job:
        logger.info('No jobs waiting!')
        return

    # Basic Setup
    client = None
    container = None
    browsertrix_life_cycle_thread = None
    have_archive = False
    filename = f'{uuid.uuid4()}.wacz'
    browsertrix_outputfile = f'{settings.SERVICES_DIR}/browsertrix/data/{filename}'

    try:
        inc_progress(capture_job, 0, "Validating.")
        try:
            capture_job.validated_url = validate_and_clean_url(capture_job.requested_url)
        except ValidationError as e:
            capture_job.mark_invalid({"requested_url": e.messages})
            raise HaltCaptureException
        capture_job.save()

        inc_progress(capture_job, 1, "Connecting to Docker.")
        client = docker.from_env()

        inc_progress(capture_job, 1, "Creating browsertrix container.")
        container = client.containers.create(
            settings.BROWSERTRIX_IMAGE,
            environment=[
                f'DATA_DIR={settings.BROWSERTRIX_INTERNAL_DATA_DIR}'
            ],
            volumes={
                settings.BROWSERTRIX_HOST_DATA_DIR : {
                    'bind': settings.BROWSERTRIX_INTERNAL_DATA_DIR,
                },
                settings.BROWSERTRIX_ENTRYPOINT : {
                    'bind': '/entrypoint.sh'
                }
            },
            entrypoint='/entrypoint.sh',
            command=f'for i in 1 2 3 4 5; do (echo status $i; sleep 1); done; cp {settings.BROWSERTRIX_INTERNAL_DATA_DIR}/examples/$(ls {settings.BROWSERTRIX_INTERNAL_DATA_DIR}/examples | shuf -n 1) {settings.BROWSERTRIX_INTERNAL_DATA_DIR}/{filename}',
            detach=True
        )

        inc_progress(capture_job, 1, "Starting browsertrix.")
        container.start()
        browsertrix_life_cycle_thread = BrowsertrixLifeCycleThread(container, settings.BROWSERTRIX_TIMEOUT_SECONDS, name="browsertrix_return")
        browsertrix_life_cycle_thread.start()
        stdout_stream = container.logs(stderr=False, stream=True)
        for msg in stdout_stream:
            # we should look into whether the browsertrix container needs init, and whether it passes signals correctly, etc.
            # see also https://github.com/moby/moby/issues/37663.
            msg = str(msg, 'utf-8').strip()
            if True:
                # if the msg matches some expectation (I'm presuming output will be noisy),
                # indicate we've reached the next step of the capture
                inc_progress(capture_job, 1, f"Browsertrix: {msg}.")
            else:
                logger.debug(msg)

        browsertrix_life_cycle_thread.join()
        if browsertrix_life_cycle_thread.exit_code != 0:
            # see there's anything we can salvage
            if os.path.isfile(browsertrix_outputfile):
                # should probably also make sure its a valid wacz?
                # a likely advantage of warcs over wacz is that you can still play back a truncated warc
                have_archive = True
            # this is NOT how we want to handle the verbose output of stderr. What's the best way to log?
            # send a special error email?
            logger.error(f"Browsertrix exited with {browsertrix_life_cycle_thread.exit_code}: {browsertrix_life_cycle_thread.stderr}")
            raise HaltCaptureException
        have_archive = True

    except HaltCaptureException:
        logger.info("HaltCaptureException thrown.")
    except SoftTimeLimitExceeded:
        logger.warning(f"Soft timeout while capturing job {capture_job.id}.")
        # might include code here for politely asking Browsertrix to stop recording.
    except:  # noqa
        logger.exception(f"Exception while capturing job {capture_job.id}:")
    finally:
        try:
            if container:
                # For now, just kill the container. We might want something gentler.
                container.remove(force=True)
            if client:
                client.close()
            if have_archive:
                archive = Archive(capture_job=capture_job)
                with open(browsertrix_outputfile , 'rb') as file:
                    inc_progress(capture_job, 1, "Processing archive.")
                    archive.hash, archive.hash_algorithm = get_file_hash(file)
                    assert not file.read()
                    archive.warc_size = file.tell()

                    inc_progress(capture_job, 1, "Saving archive.")
                    file.seek(0)
                    storage = get_storage()
                    filename = storage.save(filename, file)
                    archive.download_url = storage.url(filename)
                    archive.download_expiration_timestamp = datetime_from_timestamp(parse_querystring(archive.download_url)['Expires'][0])
                archive.save()
                capture_job.mark_completed()
                logger.info("Capture succeeded.")
            else:
                logger.info("Capture failed.")
        except:  # noqa
            logger.exception(f"Exception while finishing job {capture_job.id}:")
        finally:
            if capture_job.status == CaptureJob.Status.IN_PROGRESS:
                capture_job.mark_failed('Failed during capture.')
    run_next_capture.apply_async()


@shared_task(bind=True, max_retries=settings.WEBHOOK_MAX_RETRIES)
def dispatch_webhook(self, subscription_id, capture_job_id):
    """
    Send a webhook notification to the requested callback.

    Given:
    >>> archive, webhook_callback_factory, django_settings, mocker, mailoutbox, caplog = [getfixture(i) for i in ['no_signals_archive', 'webhook_callback_factory', 'settings', 'mocker', 'mailoutbox', 'caplog']]

    We send a serialization of the webhook subscription and the capture job, and we
    include a signature of the payload in the HTTP headers. We expect a response with
    a status code of 200 or 204.

    >>> for status in [200, 204]:
    ...     webhook, mock = webhook_callback_factory(status)
    ...     _ = dispatch_webhook.apply([webhook.id, archive.capture_job.id])
    ...     payload =  mock.last_request.json()
    ...     assert all(key in payload for key in ['webhook', 'capture_job'])
    ...     assert is_valid_signature(mock.last_request.headers['x-hook-signature'], payload, webhook.signing_key, webhook.signing_key_algorithm)
    ...     assert f'Webhook notification for subscription {webhook.id}, capture job {archive.capture_job.id} delivered.' in caplog.text
    ...     caplog.clear()

    We retry if the callback sends an unexpected status code.

    >>> for status in [301, 302, 400, 401, 413, 500, 502]:
    ...     webhook, mock = webhook_callback_factory(status)
    ...     with assert_raises(Retry):
    ...         dispatch_webhook.apply([webhook.id, archive.capture_job.id])
    ...     assert mock.called

    If we hit the retry limit, we email the user to let them know their hook is
    failing, including the ID of the archive, so that they can use the API to
    retrieve its info and recover.

    >>> retry = mocker.patch.object(dispatch_webhook, 'retry')
    >>> retry.side_effect = MaxRetriesExceededError()
    >>> webhook, mock = webhook_callback_factory(502)
    >>> _ = dispatch_webhook.apply([webhook.id, archive.capture_job.id])
    >>> [email] = mailoutbox
    >>> assert "webhook notification failed" in email.subject
    >>> assert f"capture job {archive.capture_job.id}" in email.body
    >>> mock.reset()
    >>> caplog.clear()

    If necessary, the sendind of webhook notifications can be disabled via a Django setting.

    >>> django_settings.DISPATCH_WEBHOOKS = False
    >>> _ = dispatch_webhook.apply([webhook.id, archive.capture_job.id])
    >>> assert 'Webhooks notifications are disabled' in caplog.text
    >>> assert not mock.called
    """
    if not settings.DISPATCH_WEBHOOKS:
        logger.info(f'Webhooks notifications are disabled: not sending POST for subscription {subscription_id}, capture job {capture_job_id}.')
        return

    subscription = WebhookSubscription.objects.get(id=subscription_id)
    capture_job = CaptureJob.objects.filter(id=capture_job_id).select_related('archive').first()

    payload = {
        "webhook": SimpleWebhookSubscriptionSerializer(subscription).data,
        "capture_job": ReadOnlyCaptureJobSerializer(capture_job).data
    }
    try:
        response = requests.post(
            url=subscription.callback_url,
            json=payload,
            headers={'x-hook-signature': sign_data(payload, subscription.signing_key, subscription.signing_key_algorithm)},
            timeout=20,
            allow_redirects=False
        )
        assert response.status_code in [200, 204], response.status_code
    except (requests.RequestException, AssertionError):
        logger.info(f'Delivery of webhook notification for subscription {subscription_id}, capture job {capture_job_id} failed ({self.request.retries}/{self.max_retries}).')
        try:
            # retry with exponential backoff, up to settings.WEBHOOK_MAX_RETRIES times
            self.retry(countdown=2**self.request.retries)
        except MaxRetriesExceededError:
            logger.warning(f'Delivery of webhook notification for subscription {subscription_id}, capture job {capture_job_id} permanently failed.')
            send_template_email(
                f"[ALERT] Your {settings.APP_NAME} webhook notification failed.",
                'email/webhook_failed.txt',
                {"subscription": subscription, "capture_job": capture_job},
                settings.DEFAULT_FROM_EMAIL,
                [subscription.user.email],
            )
            return
    logger.info(f'Webhook notification for subscription {subscription_id}, capture job {capture_job_id} delivered.')
