============================
Getting Started With Proxmox
============================

Proxmox Virtual Environment is a complete server virtualization management solution,
based on LXC and full virtualization with KVM.
Further information can be found at:

http://www.proxmox.org/

Dependencies
============
* IPy >= 0.81
* requests >= 2.2.1

Please note:
This module allows you to create both OpenVZ and KVM but installing Salt on it will only be
done when the VM is an OpenVZ container rather than a KVM virtual machine.

* Set up the cloud configuration at
  ``/etc/salt/cloud.providers`` or
  ``/etc/salt/cloud.providers.d/proxmox.conf``:

.. code-block:: yaml

    my-proxmox-config:
      # Set up the location of the salt master
      #
      minion:
        master: saltmaster.example.com

      # Set the PROXMOX access credentials (see below)
      #
      user: myuser@pve
      password: badpass

      # Set the access URL for your PROXMOX host
      #
      url: your.proxmox.host
      driver: proxmox

.. note::
    .. versionchanged:: 2015.8.0

    The ``provider`` parameter in cloud provider definitions was renamed to ``driver``. This
    change was made to avoid confusion with the ``provider`` parameter that is used in cloud profile
    definitions. Cloud provider definitions now use ``driver`` to refer to the Salt cloud module that
    provides the underlying functionality to connect to a cloud host, while cloud profiles continue
    to use ``provider`` to refer to provider configurations that you define.

Access Credentials
==================
The ``user``, ``password``, and ``url`` will be provided to you by your cloud
host. These are all required in order for the PROXMOX driver to work.


Cloud Profiles
==============
Set up an initial profile at ``/etc/salt/cloud.profiles`` or
``/etc/salt/cloud.profiles.d/proxmox.conf``:

* Configure a profile to be used:

.. code-block:: yaml

    proxmox-ubuntu:
        provider: my-proxmox-config
        image: local:vztmpl/ubuntu-12.04-standard_12.04-1_amd64.tar.gz
        technology: openvz

        # host needs to be set to the configured name of the proxmox host
        # and not the ip address or FQDN of the server
        host: myvmhost
        ip_address: 192.168.100.155
        password: topsecret


The profile can be realized now with a salt command:

.. code-block:: bash

    # salt-cloud -p proxmox-ubuntu myubuntu

This will create an instance named ``myubuntu`` on the cloud host. The
minion that is installed on this instance will have a ``hostname`` of ``myubuntu``.
If the command was executed on the salt-master, its Salt key will automatically
be signed on the master.

Once the instance has been created with salt-minion installed, connectivity to
it can be verified with Salt:

.. code-block:: bash

    # salt myubuntu test.ping


Required Settings
=================
The following settings are always required for PROXMOX:

* Using the new cloud configuration format:

.. code-block:: yaml

    my-proxmox-config:
      driver: proxmox
      user: saltcloud@pve
      password: xyzzy
      url: your.proxmox.host

Optional Settings
=================
Unlike other cloud providers in Salt Cloud, Proxmox does not utilize a
``size`` setting. This is because Proxmox allows the end-user to specify a
more detailed configuration for their instances, than is allowed by many other
cloud providers. The following options are available to be used in a profile,
with their default settings listed.

.. code-block:: yaml

    # Description of the instance.
    desc: <instance_name>

    # How many CPU cores, and how fast they are (in MHz)
    cpus: 1
    cpuunits: 1000

    # How many megabytes of RAM
    memory: 256

    # How much swap space in MB
    swap: 256

    # Whether to auto boot the vm after the host reboots
    onboot: 1

    # Size of the instance disk (in GiB)
    disk: 10

    # Host to create this vm on
    host: myvmhost

    # Nameservers. Defaults to host
    nameserver: 8.8.8.8 8.8.4.4

    # Username and password
    ssh_username: root
    password: <value from PROXMOX.password>

    # The name of the image, from ``salt-cloud --list-images proxmox``
    image: local:vztmpl/ubuntu-12.04-standard_12.04-1_amd64.tar.gz
