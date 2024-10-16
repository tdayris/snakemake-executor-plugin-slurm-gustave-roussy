__author__ = "Thibault Dayris"
__copyright__ = "Copyright 2024, Thibault Dayris"
__email__ = "thibault.dayris@gustaveroussy.fr"
__license__ = "MIT"

# This executor is highly based on snakemake-executor-plugin-slurm
# its purpose is to be used on and only on Gustave Roussy's computing
# cluster. (Flamingo, not the old ones)

import csv
import os
import subprocess
import time
import uuid

from dataclasses import dataclass, field
from io import StringIO
from typing import List, Generator, Optional
from snakemake_interface_executor_plugins.executors.base import SubmittedJobInfo
from snakemake_interface_executor_plugins.executors.remote import RemoteExecutor
from snakemake_interface_executor_plugins.settings import (
    ExecutorSettingsBase,
    CommonSettings,
)
from snakemake_interface_executor_plugins.jobs import (
    JobExecutorInterface,
)
from snakemake_interface_common.exceptions import WorkflowError  # noqa


# Optional:
# Define additional settings for your executor.
# They will occur in the Snakemake CLI as --<executor-name>-<param-name>
# Omit this class if you don't need any.
# Make sure that all defined fields are Optional and specify a default value
# of None or anything else that makes sense in your case.
@dataclass
class ExecutorSettings(ExecutorSettingsBase):
    args: Optional[str] = field(
        default=None,
        metadata={"help": "Optional arguments passed to slurm"},
    )


# Required:
# Specify common settings shared by various executors.
common_settings = CommonSettings(
    # define whether your executor plugin executes locally
    # or remotely. In virtually all cases, it will be remote execution
    # (cluster, cloud, etc.). Only Snakemake's standard execution
    # plugins (snakemake-executor-plugin-dryrun, snakemake-executor-plugin-local)
    # are expected to specify False here.
    non_local_exec=True,
    # Whether the executor implies to not have a shared file system
    implies_no_shared_fs=False,
    # whether to deploy workflow sources to default storage provider before execution
    job_deploy_sources=False,
    # whether arguments for setting the storage provider shall be passed to jobs
    pass_default_storage_provider_args=True,
    # whether arguments for setting default resources shall be passed to jobs
    pass_default_resources_args=True,
    # whether environment variables shall be passed to jobs (if False, use
    # self.envvars() to obtain a dict of environment variables and their values
    # and pass them e.g. as secrets to the execution backend)
    pass_envvar_declarations_to_cmd=False,
    # whether the default storage provider shall be deployed before the job is run on
    # the remote node. Usually set to True if the executor does not assume a shared fs
    auto_deploy_default_storage_provider=False,
    # specify initial amount of seconds to sleep before checking for job status
    init_seconds_before_status_checks=40,
    pass_group_args=True,
)


# Required:
# Implementation of your executor
class Executor(RemoteExecutor):
    def __post_init__(self):
        self.run_uuid: str = str(uuid.uuid4())
        self.logger.info(f"SLURM run ID: {self.run_uuid}")
        self._default_runtime: int = 360
        self._default_mem: int = 1024
        self._fallback_account_arg: Optional[str] = None
        self._fallback_partition: str = "shortq"
        self._max_sleep_time: int = 120

        # access workflow
        # self.workflow
        # access executor specific settings
        # self.workflow.executor_settings

        # IMPORTANT: in your plugin, only access methods and properties of
        # Snakemake objects (like Workflow, Persistence, etc.) that are
        # defined in the interfaces found in the
        # snakemake-interface-executor-plugins and the
        # snakemake-interface-common package.
        # Other parts of those objects are NOT guaranteed to remain
        # stable across new releases.

        # To ensure that the used interfaces are not changing, you should
        # depend on these packages as >=a.b.c,<d with d=a+1 (i.e. pin the
        # dependency on this package to be at least the version at time
        # of development and less than the next major version which would
        # introduce breaking changes).

        # In case of errors outside of jobs, please raise a WorkflowError

    def additional_general_args(self) -> str:
        return "--executor slurm-jobstep --jobs 1"

    def get_partition(
        self, runtime: int = 360, gres: Optional[str] = None, mem: int = 1_000
    ) -> str:
        """
        Return best partition based on runtime reservation
        and possible GPU resources reservation.
        """
        hostname: str = os.environ.get("HOSTNAME", "").lower()
        if hostname.startswith("flamingo"):
            print("Pipeline should not be launched from login node.")

        if hostname in [f"n{i:0=2d}" for i in range(1, 26)] + [
            f"gpu{i:0=2d}" for i in range(1, 4)
        ]:
            if gres:
                queue, node_type, gpu_number = gres.split(":")
                if node_type.lower().strip() in ("a100", "v100"):
                    return "gpgpuq"
                if node_type.lower().strip() == "t4":
                    return "visuq"

                return "gpuq"

            if mem > 500_000:
                return "bigmemq"

            if runtime <= 360:
                return "shortq"

            if runtime <= 60 * 24:
                return "mediumq"

            if runtime <= 60 * 24 * 7:
                return "longq"

            if runtime <= 60 * 24 * 60:
                return "verylongq"

            self.logger.warning(
                "Could not select a correct partition. "
                f"Falling back to default: {self._fallback_partition}"
            )
            return self._fallback_partition
        return None

    def get_node(self, gres: Optional[str] = None) -> str:
        """
        Return best node according to resources
        """
        if gres is None:
            return "default"

        queue, node_type, gpu_number = gres.split(":")
        if node_type.lower().strip() == "a100":
            return "gpu03"
        if node_type.lower().strip() == "t4":
            return "gpu01"
        if node_type.lower().strip() == "v100":
            return "gpu02"

        return "default"

    def run_job(self, job: JobExecutorInterface):
        # Implement here how to run a job.
        # You can access the job's resources, etc.
        # via the job object.
        # After submitting the job, you have to call
        # self.report_job_submission(job_info).
        # with job_info being of type
        # snakemake_interface_executor_plugins.executors.base.SubmittedJobInfo.
        # If required, make sure to pass the job's id to the job_info object, as keyword
        # argument 'external_job_id'.

        slurm_logfile = job.resources.get("logfile")
        if slurm_logfile is None:
            log_folder: str = (
                f"group_{job.name}" if job.is_group() else f"rule_{job.name}"
            )
            slurm_logfile: str = os.path.abspath(
                f".snakemake/slurm_logs/{log_folder}/%j.log"
            )
        os.makedirs(os.path.dirname(slurm_logfile), exist_ok=True)

        # generic part of a submission string:
        # we use a run_uuid as the job-name, to allow `--name`-based
        # filtering in the job status checks (`sacct --name` and `squeue --name`)
        call: str = (
            f"sbatch --job-name {self.run_uuid} --output {slurm_logfile} --export=ALL "
            f"--comment {job.name} "
        )

        memory_word: str = "mem_mb"
        if "mem" in job.resources.keys():
            memory_word = "mem"
        if "mem_gb" in job.resources.keys():
            memory_word = "mem_gb"
        memory: int = job.resources.get(memory_word, self._default_mem)
        call += f" --mem {memory} "

        runtime_word: str = "runtime"
        if "time_min" in job.resources.keys():
            runtime_word = "time_min"
        elif "walltime" in job.resources.keys():
            runtime_word = "walltime"
        runtime: int = job.resources.get(runtime_word, self._default_runtime)
        partition: str = self.get_partition(
            runtime=runtime,
            gres=job.resources.get("gres"),
            mem=memory,
        )
        if partition:
            call += f" --partition {partition} "

        # node_name: str = self.get_node(job.resources.get("gres"))
        # if node_name != "default":
        #    call += f" --nodelist='{node_name}' "

        call += f"--time {runtime} --cpus-per-task {job.threads}"

        # ensure that workdir is set correctly
        # use short argument as this is the same in all slurm versions
        # (see https://github.com/snakemake/snakemake/issues/2014)
        call += f" -D {self.workflow.workdir_init}"
        # and finally the job to execute with all the snakemake parameters
        exec_job: str = self.format_job_exec(job)
        call += f' --wrap="{exec_job}"'

        self.logger.debug(f"sbatch call: {call}")
        try:
            out: str = subprocess.check_output(
                call, shell=True, text=True, stderr=subprocess.STDOUT
            ).strip()
        except subprocess.CalledProcessError as e:
            raise WorkflowError(
                f"SLURM job submission failed. The error message was {e.output}"
            )

        slurm_jobid: str = out.split(" ")[-1]
        slurm_logfile: str = slurm_logfile.replace("%j", slurm_jobid)
        self.logger.info(
            f"Job {job.jobid} has been submitted with SLURM jobid {slurm_jobid} "
            f"(log: {slurm_logfile})."
        )
        self.report_job_submission(
            SubmittedJobInfo(
                job, external_jobid=slurm_jobid, aux={"slurm_logfile": slurm_logfile}
            )
        )

    async def check_active_jobs(
        self, active_jobs: List[SubmittedJobInfo]
    ) -> Generator[SubmittedJobInfo, None, None]:
        # Check the status of active jobs.

        # You have to iterate over the given list active_jobs.
        # For jobs that have finished successfully, you have to call
        # self.report_job_success(job).
        # For jobs that have errored, you have to call
        # self.report_job_error(job).
        # Jobs that are still running have to be yielded.
        #
        # For queries to the remote middleware, please use
        # self.status_rate_limiter like this:
        #
        # async with self.status_rate_limiter:
        #    # query remote middleware here
        fail_status: List[str] = (
            "BOOT_FAIL",
            "CANCELLED",
            "DEADLINE",
            "FAILED",
            "NODE_FAIL",
            "OUT_OF_MEMORY",
            "PREEMPTED",
            "TIMEOUT",
            "ERROR",
        )
        # Cap sleeping time between querying the status of all active jobs:
        # If `AccountingStorageType`` for `sacct` is set to `accounting_storage/none`,
        # sacct will query `slurmctld` (instead of `slurmdbd`) and this in turn can
        # rely on default config, see: https://stackoverflow.com/a/46667605
        # This config defaults to `MinJobAge=300`, which implies that jobs will be
        # removed from `slurmctld` within 6 minutes of finishing. So we're conservative
        # here, with half that time
        max_sleep_time = 180

        sacct_query_durations = []

        status_attempts = 5

        active_jobs_ids = {job_info.external_jobid for job_info in active_jobs}
        active_jobs_seen_by_sacct = set()

        # this code is inspired by the snakemake profile:
        # https://github.com/Snakemake-Profiles/slurm
        for i in range(status_attempts):
            async with self.status_rate_limiter:
                (status_of_jobs, sacct_query_duration) = await self.job_status(
                    # -X: only show main job, no substeps
                    f"sacct -X --parsable2 --noheader --format=JobIdRaw,State "
                    f"--starttime now-2days --endtime now --name {self.run_uuid}"
                )
                if status_of_jobs is None and sacct_query_duration is None:
                    self.logger.debug(f"could not check status of job {self.run_uuid}")
                    continue
                sacct_query_durations.append(sacct_query_duration)
                self.logger.debug(f"status_of_jobs after sacct is: {status_of_jobs}")
                # only take jobs that are still active
                active_jobs_ids_with_current_sacct_status = (
                    set(status_of_jobs.keys()) & active_jobs_ids
                )
                self.logger.debug(
                    f"active_jobs_ids_with_current_sacct_status are: "
                    f"{active_jobs_ids_with_current_sacct_status}"
                )
                active_jobs_seen_by_sacct = (
                    active_jobs_seen_by_sacct
                    | active_jobs_ids_with_current_sacct_status
                )
                self.logger.debug(
                    f"active_jobs_seen_by_sacct are: {active_jobs_seen_by_sacct}"
                )
                missing_sacct_status = (
                    active_jobs_seen_by_sacct
                    - active_jobs_ids_with_current_sacct_status
                )
                self.logger.debug(f"missing_sacct_status are: {missing_sacct_status}")
                if not missing_sacct_status:
                    break
            if i >= status_attempts - 1:
                self.logger.warning(
                    f"Unable to get the status of all active_jobs that should be "
                    f"in slurmdbd, even after {status_attempts} attempts.\n"
                    f"The jobs with the following slurm job ids were previously seen "
                    "by sacct, but sacct doesn't report them any more:\n"
                    f"{missing_sacct_status}\n"
                    f"Please double-check with your slurm cluster administrator, that "
                    "slurmdbd job accounting is properly set up.\n"
                )

        any_finished = False
        for j in active_jobs:
            # the job probably didn't make it into slurmdbd yet, so
            # `sacct` doesn't return it
            if j.external_jobid not in status_of_jobs:
                # but the job should still be queueing or running and
                # appear in slurmdbd (and thus `sacct` output) later
                yield j
                continue
            status = status_of_jobs[j.external_jobid]
            if status == "COMPLETED":
                self.report_job_success(j)
                any_finished = True
                active_jobs_seen_by_sacct.remove(j.external_jobid)
            elif status == "UNKNOWN":
                # the job probably does not exist anymore, but 'sacct' did not work
                # so we assume it is finished
                self.report_job_success(j)
                any_finished = True
                active_jobs_seen_by_sacct.remove(j.external_jobid)
            elif status in fail_status:
                msg = (
                    f"SLURM-job '{j.external_jobid}' failed, SLURM status is: "
                    f"'{status}'"
                )
                self.report_job_error(j, msg=msg, aux_logs=[j.aux["slurm_logfile"]])
                active_jobs_seen_by_sacct.remove(j.external_jobid)
            else:  # still running?
                yield j

        if not any_finished:
            self.next_seconds_between_status_checks = min(
                self.next_seconds_between_status_checks + 10, max_sleep_time
            )
        else:
            self.next_seconds_between_status_checks = None

    def cancel_jobs(self, active_jobs: List[SubmittedJobInfo]):
        # Cancel all active jobs.
        # This method is called when Snakemake is interrupted.
        if active_jobs:
            # TODO chunk jobids in order to avoid too long command lines
            jobids = " ".join([job_info.external_jobid for job_info in active_jobs])
            try:
                # timeout set to 60, because a scheduler cycle usually is
                # about 30 sec, but can be longer in extreme cases.
                # Under 'normal' circumstances, 'scancel' is executed in
                # virtually no time.
                subprocess.check_output(
                    f"scancel {jobids}",
                    text=True,
                    shell=True,
                    timeout=60,
                    stderr=subprocess.PIPE,
                )
            except subprocess.TimeoutExpired:
                self.logger.warning("Unable to cancel jobs within a minute.")

    async def job_status(self, command):
        """Obtain SLURM job status of all submitted jobs with sacct

        Keyword arguments:
        command -- a slurm command that returns one line for each job with:
                   "<raw/main_job_id>|<long_status_string>"
        """
        res = query_duration = None
        try:
            time_before_query = time.time()
            command_res = subprocess.check_output(
                command, text=True, shell=True, stderr=subprocess.PIPE
            )
            query_duration = time.time() - time_before_query
            self.logger.debug(
                f"The job status was queried with command: {command}\n"
                f"It took: {query_duration} seconds\n"
                f"The output is:\n'{command_res}'\n"
            )
            res = {
                # We split the second field in the output, as the State field
                # could contain info beyond the JOB STATE CODE according to:
                # https://slurm.schedmd.com/sacct.html#OPT_State
                entry[0]: entry[1].split(sep=None, maxsplit=1)[0]
                for entry in csv.reader(StringIO(command_res), delimiter="|")
            }
        except subprocess.CalledProcessError as e:
            self.logger.error(
                f"The job status query failed with command: {command}\n"
                f"Error message: {e.stderr.strip()}\n"
            )
            pass

        return (res, query_duration)
