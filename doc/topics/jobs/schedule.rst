
In Salt versions greater than 0.12.0, the scheduling system allows incremental
executions on minions or the master. The schedule system exposes the execution
of any execution function on minions or any runner on the master.

Scheduling can be enabled by multiple methods:

- ``schedule`` option in either the master or minion config files.  These
  require the master or minion application to be restarted in order for the
  schedule to be implemented.

- Minion pillar data.  Schedule is implemented by refreshing the minion's pillar data,
  for example by using ``saltutil.refresh_pillar``.
  
- The :doc:`schedule state</ref/states/all/salt.states.schedule>` or :doc:`schedule module</ref/modules/all/salt.modules.schedule>`

.. note::

    The scheduler executes different functions on the master and minions. When
    running on the master the functions reference runner functions, when
    running on the minion the functions specify execution functions.

A scheduled run has no output on the minion unless the config is set to info level
or higher. Refer to :doc:`minion logging settings</ref/configuration/minion>`.

Specify ``maxrunning`` to ensure that there are no more than N copies of
a particular routine running.  Use this for jobs that may be long-running
and could step on each other or otherwise double execute.  The default for
``maxrunning`` is 1.

States are executed on the minion, as all states are. You can pass positional
arguments and provide a yaml dict of named arguments.

The below example will schedule the command ``state.apply httpd test=True``
every 3600 seconds (every hour):

.. code-block:: yaml

    schedule:
      job1:
        function: state.apply
        seconds: 3600
        args:
          - httpd
        kwargs:
          test: True

This next example will schedule the command ``state.apply httpd test=True``
every 3600 seconds (every hour) splaying the time between 0 and 15 seconds:

.. code-block:: yaml

    schedule:
      job1:
        function: state.apply
        seconds: 3600
        args:
          - httpd
        kwargs:
          test: True
        splay: 15

Finally, the next example will schedule the command ``state.apply httpd
test=True`` every 3600 seconds (every hour) splaying the time between 10 and 15
seconds:

.. code-block:: yaml

    schedule:
      job1:
        function: state.apply
        seconds: 3600
        args:
          - httpd
        kwargs:
          test: True
        splay:
          start: 10
          end: 15

.. versionadded:: 2014.7.0

Frequency of jobs can also be specified using date strings supported by
the python dateutil library. This requires python-dateutil to be installed on
the minion.

For example, this will schedule the command ``state.apply httpd test=True`` at
5:00pm localtime on the minion.

.. code-block:: yaml

    schedule:
      job1:
        function: state.apply
        args:
          - httpd
        kwargs:
          test: True
        when: 5:00pm

To schedule the command ``state.apply httpd test=True`` at 5pm on Monday,
Wednesday, and Friday, and 3pm on Tuesday and Thursday, use the following:

.. code-block:: yaml

    schedule:
      job1:
        function: state.apply
        args:
          - httpd
        kwargs:
          test: True
        when:
            - Monday 5:00pm
            - Tuesday 3:00pm
            - Wednesday 5:00pm
            - Thursday 3:00pm
            - Friday 5:00pm

Time ranges are also supported. For example, the below configuration will
schedule the command ``state.apply httpd test=True`` every 3600 seconds (every
hour) between the hours of 8am and 5pm. The range parameter must be a
dictionary with the date strings using the ``dateutil`` format.

.. code-block:: yaml

    schedule:
      job1:
        function: state.apply
        seconds: 3600
        args:
          - httpd
        kwargs:
          test: True
        range:
            start: 8:00am
            end: 5:00pm

.. note::
    Using time ranges requires python-dateutil_ to be installed on the minion.

.. _python-dateutil: https://github.com/dateutil/dateutil#dateutil---powerful-extensions-to-datetime

.. versionadded:: 2014.7.0

The scheduler also supports ensuring that there are no more than N copies of a
particular routine running. Use this for jobs that may be long-running and
could step on each other or pile up in case of infrastructure outage.

The default for ``maxrunning`` is 1.

.. code-block:: yaml

    schedule:
      long_running_job:
          function: big_file_transfer
          jid_include: True

run_on_start
------------

.. versionadded:: 2015.5.0

By default, any job scheduled based on the startup time of the minion will run
the scheduled job when the minion starts up. Sometimes this is not the desired
situation. Using the ``run_on_start`` parameter set to ``False`` will cause the
scheduler to skip this first run and wait until the next scheduled run.

.. code-block:: yaml

    schedule:
      job1:
        function: state.sls
        seconds: 3600
        run_on_start: False
        args:
          - httpd
        kwargs:
          test: True


States
======

.. code-block:: yaml

    schedule:
      log-loadavg:
        function: cmd.run
        seconds: 3660
        args:
          - 'logger -t salt < /proc/loadavg'
        kwargs:
          stateful: False
          shell: /bin/sh

Highstates
==========

To set up a :ref:`highstate <running-highstate>` to run on a minion every 60
minutes set this in the minion config or pillar:

.. code-block:: yaml

    schedule:
      highstate:
        function: state.apply
        minutes: 60

Time intervals can be specified as seconds, minutes, hours, or days.

Runners
=======

Runner executions can also be specified on the master within the master
configuration file:

.. code-block:: yaml

    schedule:
      run_my_orch:
        function: state.orchestrate
        hours: 6
        splay: 600
        args:
          - orchestration.my_orch

The above configuration is analogous to running
``salt-run state.orch orchestration.my_orch`` every 6 hours.

Scheduler With Returner
=======================

The scheduler is also useful for tasks like gathering monitoring data about
a minion, this schedule option will gather status data and send it to a MySQL
returner database:

.. code-block:: yaml

    schedule:
      uptime:
        function: status.uptime
        seconds: 60
        returner: mysql
      meminfo:
        function: status.meminfo
        minutes: 5
        returner: mysql

Since specifying the returner repeatedly can be tiresome, the
``schedule_returner`` option is available to specify one or a list of global
returners to be used by the minions when scheduling.
