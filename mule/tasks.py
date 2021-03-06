from celery.task import task
from celery.worker.control import Panel
from mule import conf

import os
import subprocess
import shlex
import tempfile
import time
import traceback

__all__ = ('mule_setup', 'mule_teardown', 'run_test')

def join_queue(cset, name, **kwargs):
    queue = cset.add_consumer_from_dict(queue=name, **kwargs)
    # XXX: There's currently a bug in Celery 2.2.5 which doesn't declare the queue automatically
    channel = cset.channel
    queue(channel).declare()

    # start consuming from default
    cset.consume()

def execute_bash(name, script, workspace=None, logger=None, **env_kwargs):
    (h, script_path) = tempfile.mkstemp(prefix=name)
    
    if logger:
        logger.info('Executing %s in %s', name, script_path)

    if workspace:
        assert conf.ROOT
        work_path = os.path.join(conf.ROOT, 'workspaces', workspace)
    else:
        work_path = os.getcwd()

    with open(script_path, 'w') as fp:
        fp.write(unicode(script).encode('utf-8'))

    cmd = '/bin/bash %s' % script_path.encode('utf-8')

    # Setup our environment variables
    env = os.environ.copy()
    for k, v in env_kwargs.iteritems():
        env[unicode(k).encode('utf-8')] = unicode(v).encode('utf-8')
    env['WORKSPACE'] = work_path
    
    start = time.time()
    
    try:
        proc = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, cwd=work_path)
        (stdout, stderr) = map(lambda x: x.strip(), proc.communicate())
    except KeyboardInterrupt:
        # Ensure we propagate up the exception
        raise
    except Exception, e:
        (stdout, stderr, returncode) =  ('', 'Error running command [%s]: %s' % (cmd, traceback.format_exc()), 1)
    else:
        returncode = proc.returncode
    
    stop = time.time()
    
    if logger:
        logger.info('Script execution completed in %.3fs', stop - start)
    
    return (stdout, stderr, returncode)

@Panel.register
def mule_setup(panel, build_id, workspace=None, script=None):
    """
    This task has two jobs:

    1. Leaves the default Mule queue, and joins a new build-specific queue.

    2. Ensure that we're bootstrapped for this build.

       This includes:
         - Doing a git fetch
         - Setting up a virtualenv
         - Building our DB
    """
    assert not script or workspace, "Cannot pass scripts without a workspace"
    
    queue_name = '%s-%s' % (conf.BUILD_QUEUE_PREFIX, build_id)

    cset = panel.consumer.task_consumer
    
    if conf.DEFAULT_QUEUE not in [q.name for q in cset.queues]:
        return {
            "status": "fail",
            "reason": "worker is already in use"
        }
    
    cset.cancel_by_queue(conf.DEFAULT_QUEUE)
    
    script_result = ('', '', 0)
    
    if script:
        try:
            script_result = execute_bash(
                name='setup.sh',
                script=script,
                workspace=workspace,
                logger=panel.logger,
                BUILD_ID=build_id,
            )
        except:
            # If our teardown fails we need to ensure we rejoin the queue
            join_queue(cset, name=conf.DEFAULT_QUEUE)
            raise
    
    join_queue(cset, name=queue_name, exchange_type='direct')

    panel.logger.info("Started consuming from %s", queue_name)

    return {
        "status": "ok",
        "build_id": build_id,
        "stdout": script_result[0],
        "stderr": script_result[1],
        "retcode": script_result[2],
    }

@Panel.register
def mule_teardown(panel, build_id, workspace=None, script=None):
    """
    This task has two jobs:
    
    1. Run any bootstrap teardown

    2. Leaves the build-specific queue, and joins the default Mule queue.
    """
    assert not script or workspace, "Cannot pass scripts without a workspace"
    
    queue_name = '%s-%s' % (conf.BUILD_QUEUE_PREFIX, build_id)

    cset = panel.consumer.task_consumer
    channel = cset.channel
    # kill all jobs in queue
    channel.queue_purge(queue=queue_name)
    # stop consuming from queue
    cset.cancel_by_queue(queue_name)
    
    script_result = ('', '', 0)
    
    if script:
        try:
            script_result = execute_bash(
                name='teardown.sh',
                script=script,
                workspace=workspace,
                logger=panel.logger,
                BUILD_ID=build_id,
            )
        except:
            # If our teardown fails we need to ensure we rejoin the queue
            join_queue(cset, name=conf.DEFAULT_QUEUE)
            raise
    
    join_queue(cset, name=conf.DEFAULT_QUEUE)

    panel.logger.info("Rejoined default queue")

    return {
        "status": "ok",
        "build_id": build_id,
        "stdout": script_result[0],
        "stderr": script_result[1],
        "retcode": script_result[2],
    }


@task(ignore_result=False)
def run_test(build_id, runner, job, callback=None, workspace=None):
    """
    Spawns a test runner and reports the result.
    """
    start = time.time()

    script_result = execute_bash(
        name='test.sh',
        script=runner,
        workspace=workspace,
        logger=run_test.get_logger(),
        BUILD_ID=build_id,
        TEST=job,
    )

    stop = time.time()

    result = {
        "timeStarted": start,
        "timeFinished": stop,
        "build_id": build_id,
        "job": job,
        "stdout": script_result[0],
        "stderr": script_result[1],
        "retcode": script_result[2],
    }

    if callback:
        callback(result)

    return result