from datetime import timedelta
from typing import Dict, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.configurations import RegistryAuth
from dstack._internal.core.models.repos import RemoteRepoCreds
from dstack._internal.core.models.runs import Job, JobErrorCode, JobStatus, Run
from dstack._internal.server.db import get_session_ctx
from dstack._internal.server.models import JobModel, RepoModel, RunModel
from dstack._internal.server.services import logs as logs_services
from dstack._internal.server.services.jobs import (
    RUNNING_PROCESSING_JOBS_IDS,
    RUNNING_PROCESSING_JOBS_LOCK,
    job_model_to_job_submission,
    terminate_job_submission_instance,
)
from dstack._internal.server.services.repos import get_code_model, repo_model_to_repo_head
from dstack._internal.server.services.runner import client
from dstack._internal.server.services.runner.ssh import runner_ssh_tunnel
from dstack._internal.server.services.runs import (
    create_job_model_for_new_submission,
    run_model_to_run,
)
from dstack._internal.server.utils.common import run_async
from dstack._internal.utils import common as common_utils
from dstack._internal.utils.interpolator import VariablesInterpolator
from dstack._internal.utils.logging import get_logger

logger = get_logger(__name__)


async def process_running_jobs():
    async with get_session_ctx() as session:
        async with RUNNING_PROCESSING_JOBS_LOCK:
            res = await session.execute(
                select(JobModel)
                .where(
                    JobModel.status.in_(
                        [JobStatus.PROVISIONING, JobStatus.PULLING, JobStatus.RUNNING]
                    ),
                    JobModel.id.not_in(RUNNING_PROCESSING_JOBS_IDS),
                )
                .order_by(JobModel.last_processed_at.asc())
                .limit(1)  # TODO process multiple at once
            )
            job_model = res.scalar()
            if job_model is None:
                return

            RUNNING_PROCESSING_JOBS_IDS.add(job_model.id)

    try:
        await _process_job(job_id=job_model.id)
    finally:
        RUNNING_PROCESSING_JOBS_IDS.remove(job_model.id)


async def _process_job(job_id: UUID):
    async with get_session_ctx() as session:
        res = await session.execute(select(JobModel).where(JobModel.id == job_id))
        job_model = res.scalar_one()
        res = await session.execute(
            select(RunModel)
            .where(RunModel.id == job_model.run_id)
            .options(joinedload(RunModel.project))
            .options(joinedload(RunModel.user))
            .options(joinedload(RunModel.repo))
        )
        run_model = res.scalar()
        repo_model = run_model.repo
        project = run_model.project
        run = run_model_to_run(run_model)
        job = run.jobs[job_model.job_num]
        job_submission = job_model_to_job_submission(job_model)
        job_provisioning_data = job_submission.job_provisioning_data
        server_ssh_private_key = project.ssh_private_key
        secrets = {}  # TODO secrets
        repo_creds = repo_model_to_repo_head(repo_model, include_creds=True).repo_creds

        if (
            job_model.status == JobStatus.PROVISIONING
        ):  # fails are acceptable until timeout is exceeded
            if job_provisioning_data.dockerized:
                logger.debug("Polling provisioning job with shim: %s", job_model.job_name)
                success = await run_async(
                    _process_provisioning_with_shim,
                    server_ssh_private_key,
                    job_provisioning_data,
                    job_model,
                    secrets,
                    job.job_spec.registry_auth,
                )
            else:
                logger.debug("Polling provisioning job without shim: %s", job_model.job_name)
                code = await _get_job_code(
                    session=session,
                    repo=repo_model,
                    code_hash=run.run_spec.repo_code_hash,
                )
                success = await run_async(
                    _process_provisioning_no_shim,
                    server_ssh_private_key,
                    job_provisioning_data,
                    run,
                    job_model,
                    job,
                    code,
                    secrets,
                    repo_creds,
                )
            if not success:  # check timeout
                if job_submission.age > _get_runner_timeout_interval(
                    job_provisioning_data.backend
                ):
                    logger.warning(
                        "Job %s failed because runner has not become available in time.",
                        job_model.job_name,
                    )
                    job_model.status = JobStatus.FAILED
                    job_model.error_code = JobErrorCode.WAITING_RUNNER_LIMIT_EXCEEDED
        else:  # fails are not acceptable
            if job_model.status == JobStatus.PULLING:
                logger.debug("Polling pulling job with shim: %s", job_model.job_name)
                code = await _get_job_code(
                    session=session,
                    repo=repo_model,
                    code_hash=run.run_spec.repo_code_hash,
                )
                success = await run_async(
                    _process_pulling_with_shim,
                    server_ssh_private_key,
                    job_provisioning_data,
                    run,
                    job_model,
                    job,
                    code,
                    secrets,
                    repo_creds,
                )
            elif job_model.status == JobStatus.RUNNING:
                logger.debug("Polling running job: %s", job_model.job_name)
                success = await run_async(
                    _process_running,
                    server_ssh_private_key,
                    job_provisioning_data,
                    run_model,
                    job_model,
                )
            if not success:  # kill the job
                logger.debug("Job %s failed because runner is not available", job_model.job_name)
                job_model.status = JobStatus.FAILED
                job_model.error_code = JobErrorCode.INTERRUPTED_BY_NO_CAPACITY
                if job.is_retry_active():
                    if job_submission.job_provisioning_data.instance_type.resources.spot:
                        new_job_model = create_job_model_for_new_submission(
                            run_model=run_model,
                            job=job,
                            status=JobStatus.PENDING,
                        )
                        session.add(new_job_model)
                if (
                    job_model.error_code == JobErrorCode.INTERRUPTED_BY_NO_CAPACITY
                ):  # TODO currently always True
                    # JobErrorCode.INTERRUPTED_BY_NO_CAPACITY means that we could not connect to runner.
                    # The instance may still be running (e.g. network issue), so we force termination.
                    await terminate_job_submission_instance(
                        project=project,
                        job_submission=job_submission,
                    )
        job_model.last_processed_at = common_utils.get_current_datetime()
        await session.commit()


@runner_ssh_tunnel(ports=[client.REMOTE_RUNNER_PORT], retries=1)
def _process_provisioning_no_shim(
    run: Run,
    job_model: JobModel,
    job: Job,
    code: bytes,
    secrets: Dict[str, str],
    repo_credentials: Optional[RemoteRepoCreds],
    *,
    ports: Dict[int, int],
) -> bool:
    """
    Possible next states:
    - JobStatus.RUNNING if runner is available
    - JobStatus.FAILED if timeout is exceeded

    Returns:
        is successful
    """
    runner_client = client.RunnerClient(port=ports[client.REMOTE_RUNNER_PORT])
    resp = runner_client.healthcheck()
    if resp is None:
        return False  # runner is not available yet
    _submit_job_to_runner(
        runner_client=runner_client,
        run=run,
        job_model=job_model,
        job=job,
        code=code,
        secrets=secrets,
        repo_credentials=repo_credentials,
    )
    job_model.status = JobStatus.RUNNING
    logger.debug("Job %s is running", job_model.job_name)
    return True


@runner_ssh_tunnel(ports=[client.REMOTE_SHIM_PORT], retries=1)
def _process_provisioning_with_shim(
    job_model: JobModel,
    secrets: Dict[str, str],
    registry_auth: Optional[RegistryAuth],
    *,
    ports: Dict[int, int],
) -> bool:
    """
    Possible next states:
    - JobStatus.PULLING if shim is available
    - JobStatus.FAILED if timeout is exceeded

    Returns:
        is successful
    """
    shim_client = client.ShimClient(port=ports[client.REMOTE_SHIM_PORT])
    resp = shim_client.healthcheck()
    if resp is None:
        logger.debug("Shim is not available yet: %s", job_model.job_name)
        return False  # shim is not available yet
    if registry_auth is not None:
        logger.debug("Authenticating %s to the registry...", job_model.job_name)
        interpolate = VariablesInterpolator({"secrets": secrets}).interpolate
        shim_client.registry_auth(
            username=interpolate(registry_auth.username),
            password=interpolate(registry_auth.password),
        )
    job_model.status = JobStatus.PULLING
    logger.debug("Job %s is pulling", job_model.job_name)
    return True


@runner_ssh_tunnel(ports=[client.REMOTE_SHIM_PORT, client.REMOTE_RUNNER_PORT])
def _process_pulling_with_shim(
    run: Run,
    job_model: JobModel,
    job: Job,
    code: bytes,
    secrets: Dict[str, str],
    repo_credentials: Optional[RemoteRepoCreds],
    *,
    ports: Dict[int, int],
) -> bool:
    """
    Possible next states:
    - JobStatus.RUNNING if runner is available
    - JobStatus.FAILED if shim is not available

    Returns:
        is successful
    """
    shim_client = client.ShimClient(port=ports[client.REMOTE_SHIM_PORT])
    shim_client.pull()  # raises error if shim is down, causes retry

    runner_client = client.RunnerClient(port=ports[client.REMOTE_RUNNER_PORT])
    resp = runner_client.healthcheck()
    if resp is None:
        return True  # runner is not available yet, but shim is alive (pulling)

    _submit_job_to_runner(
        runner_client=runner_client,
        run=run,
        job_model=job_model,
        job=job,
        code=code,
        secrets=secrets,
        repo_credentials=repo_credentials,
    )
    return True


@runner_ssh_tunnel(ports=[client.REMOTE_RUNNER_PORT])
def _process_running(
    run_model: RunModel,
    job_model: JobModel,
    *,
    ports: Dict[int, int],
) -> bool:
    """
    Possible next states:
    - JobStatus.FAILED if runner is not available
    - Any status received from runner

    Returns:
        is successful
    """
    runner_client = client.RunnerClient(port=ports[client.REMOTE_RUNNER_PORT])
    timestamp = 0
    if job_model.runner_timestamp is not None:
        timestamp = job_model.runner_timestamp
    resp = runner_client.pull(timestamp)  # raises error if runner is down, causes retry
    job_model.runner_timestamp = resp.last_updated
    logs_services.write_logs(
        project=run_model.project,
        run_name=run_model.run_name,
        job_submission_id=job_model.id,
        runner_logs=resp.runner_logs,
        job_logs=resp.job_logs,
    )
    if len(resp.job_states) > 0:
        last_job_state = resp.job_states[-1]
        job_model.status = last_job_state.state
        logger.debug("Updated job %s status to %s", job_model.job_name, job_model.status)
    return True


async def _get_job_code(session: AsyncSession, repo: RepoModel, code_hash: str) -> bytes:
    code_model = await get_code_model(session=session, repo=repo, code_hash=code_hash)
    if code_model is not None:
        return code_model.blob
    return b""


def _submit_job_to_runner(
    runner_client: client.RunnerClient,
    run: Run,
    job_model: JobModel,
    job: Job,
    code: bytes,
    secrets: Dict[str, str],
    repo_credentials: Optional[RemoteRepoCreds],
):
    logger.debug("Submitting job %s...", job_model.job_name)
    logger.debug(
        "Repo credentials: %s",
        None if repo_credentials is None else repo_credentials.protocol.value,
    )
    runner_client.submit_job(
        run_spec=run.run_spec,
        job_spec=job.job_spec,
        secrets=secrets,
        repo_credentials=repo_credentials,
    )
    logger.debug("Uploading code %s...", job_model.job_name)
    runner_client.upload_code(code)
    logger.debug("Running job %s...", job_model.job_name)
    runner_client.run_job()
    job_model.status = JobStatus.RUNNING
    logger.debug("Job %s is running", job_model.job_name)


def _get_runner_timeout_interval(backend_type: BackendType) -> timedelta:
    if backend_type == BackendType.LAMBDA:
        return timedelta(seconds=1200)
    return timedelta(seconds=600)
