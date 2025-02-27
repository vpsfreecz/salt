# -*- coding: utf-8 -*-
'''
Copyright 2013 Google Inc. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Google Compute Engine Module
============================

The Google Compute Engine module. This module interfaces with Google Compute
Engine (GCE). To authenticate to GCE, you will need to create a Service Account.
To set up Service Account Authentication, follow the :ref:`gce_setup` instructions.

Example Provider Configuration
------------------------------

.. code-block:: yaml

    my-gce-config:
      # The Google Cloud Platform Project ID
      project: "my-project-id"
      # The Service ACcount client ID
      service_account_email_address: 1234567890@developer.gserviceaccount.com
      # The location of the private key (PEM format)
      service_account_private_key: /home/erjohnso/PRIVKEY.pem
      driver: gce
      # Specify whether to use public or private IP for deploy script.
      # Valid options are:
      #     private_ips - The salt-master is also hosted with GCE
      #     public_ips - The salt-master is hosted outside of GCE
      ssh_interface: public_ips

:maintainer: Eric Johnson <erjohnso@google.com>
:depends: libcloud >= 0.14.1
'''
# pylint: disable=invalid-name,function-redefined

# Import python libs
from __future__ import absolute_import
import os
import re
import stat
import pprint
import logging
import msgpack
from ast import literal_eval

# Import 3rd-party libs
# pylint: disable=import-error
try:
    from libcloud.compute.types import Provider
    from libcloud.compute.providers import get_driver
    from libcloud.loadbalancer.types import Provider as Provider_lb
    from libcloud.loadbalancer.providers import get_driver as get_driver_lb
    from libcloud.common.google import (
        ResourceInUseError,
        ResourceNotFoundError,
        )
    HAS_LIBCLOUD = True
except ImportError:
    HAS_LIBCLOUD = False
# pylint: enable=import-error

# Import salt libs
from salt.utils import namespaced_function
import salt.ext.six as six
import salt.utils.cloud
import salt.config as config
from salt.utils import http
from salt.cloud.libcloudfuncs import *  # pylint: disable=redefined-builtin,wildcard-import,unused-wildcard-import
from salt.exceptions import (
    SaltCloudSystemExit,
)

# Get logging started
log = logging.getLogger(__name__)

__virtualname__ = 'gce'

# custom UA
_UA_PRODUCT = 'salt-cloud'
_UA_VERSION = '0.2.0'

# Redirect GCE functions to this module namespace
avail_locations = namespaced_function(avail_locations, globals())
script = namespaced_function(script, globals())
destroy = namespaced_function(destroy, globals())
list_nodes = namespaced_function(list_nodes, globals())
list_nodes_full = namespaced_function(list_nodes_full, globals())
list_nodes_select = namespaced_function(list_nodes_select, globals())

GCE_VM_NAME_REGEX = re.compile(r'^(?:[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?)$')


# Only load in this module if the GCE configurations are in place
def __virtual__():
    '''
    Set up the libcloud functions and check for GCE configurations.
    '''
    if get_configured_provider() is False:
        return False

    if get_dependencies() is False:
        return False

    for provider, details in six.iteritems(__opts__['providers']):
        if 'gce' not in details:
            continue

        parameters = details['gce']
        pathname = os.path.expanduser(parameters['service_account_private_key'])

        if not os.path.exists(pathname):
            log.error(
                'The GCE service account private key {0!r} used in '
                'the {1!r} provider configuration does not exist\n'.format(
                    parameters['service_account_private_key'],
                    provider
                )
            )
            return False

        key_mode = str(
            oct(stat.S_IMODE(os.stat(pathname).st_mode))
        )

        if key_mode not in ('0400', '0600'):
            log.error(
                'The GCE service account private key {0!r} used in '
                'the {1!r} provider configuration needs to be set to '
                'mode 0400 or 0600\n'.format(
                    parameters['service_account_private_key'],
                    provider
                )
            )
            return False

    return __virtualname__


def get_configured_provider():
    '''
    Return the first configured instance.
    '''
    return config.is_provider_configured(
        __opts__,
        __active_provider_name__ or 'gce',
        ('project',
         'service_account_email_address',
         'service_account_private_key')
    )


def get_dependencies():
    '''
    Warn if dependencies aren't met.
    '''
    return config.check_driver_dependencies(
        __virtualname__,
        {'libcloud': HAS_LIBCLOUD}
    )


def get_lb_conn(gce_driver=None):
    '''
    Return a load-balancer conn object
    '''
    if not gce_driver:
        raise SaltCloudSystemExit(
            'Missing gce_driver for get_lb_conn method.'
        )
    return get_driver_lb(Provider_lb.GCE)(gce_driver=gce_driver)


def get_conn():
    '''
    Return a conn object for the passed VM data
    '''
    driver = get_driver(Provider.GCE)
    provider = get_configured_provider()
    project = config.get_cloud_config_value('project', provider, __opts__)
    email = config.get_cloud_config_value('service_account_email_address',
            provider, __opts__)
    private_key = config.get_cloud_config_value('service_account_private_key',
            provider, __opts__)
    gce = driver(email, private_key, project=project)
    gce.connection.user_agent_append('{0}/{1}'.format(_UA_PRODUCT,
                                                      _UA_VERSION))
    return gce


def _expand_item(item):
    '''
    Convert the libcloud object into something more serializable.
    '''
    ret = {}
    ret.update(item.__dict__)
    return ret


def _expand_node(node):
    '''
    Convert the libcloud Node object into something more serializable.
    '''
    ret = {}
    ret.update(node.__dict__)
    try:
        del ret['extra']['boot_disk']
    except Exception:  # pylint: disable=W0703
        pass
    zone = ret['extra']['zone']
    ret['extra']['zone'] = {}
    ret['extra']['zone'].update(zone.__dict__)
    return ret


def _expand_disk(disk):
    '''
    Convert the libcloud Volume object into something more serializable.
    '''
    ret = {}
    ret.update(disk.__dict__)
    zone = ret['extra']['zone']
    ret['extra']['zone'] = {}
    ret['extra']['zone'].update(zone.__dict__)
    return ret


def _expand_address(addy):
    '''
    Convert the libcloud GCEAddress object into something more serializable.
    '''
    ret = {}
    ret.update(addy.__dict__)
    ret['extra']['zone'] = addy.region.name
    return ret


def _expand_balancer(lb):
    '''
    Convert the libcloud load-balancer object into something more serializable.
    '''
    ret = {}
    ret.update(lb.__dict__)
    hc = ret['extra']['healthchecks']
    ret['extra']['healthchecks'] = []
    for item in hc:
        ret['extra']['healthchecks'].append(_expand_item(item))

    fwr = ret['extra']['forwarding_rule']
    tp = ret['extra']['forwarding_rule'].targetpool
    reg = ret['extra']['forwarding_rule'].region
    ret['extra']['forwarding_rule'] = {}
    ret['extra']['forwarding_rule'].update(fwr.__dict__)
    ret['extra']['forwarding_rule']['targetpool'] = tp.name
    ret['extra']['forwarding_rule']['region'] = reg.name

    tp = ret['extra']['targetpool']
    hc = ret['extra']['targetpool'].healthchecks
    nodes = ret['extra']['targetpool'].nodes
    region = ret['extra']['targetpool'].region
    zones = ret['extra']['targetpool'].region.zones

    ret['extra']['targetpool'] = {}
    ret['extra']['targetpool'].update(tp.__dict__)
    ret['extra']['targetpool']['region'] = _expand_item(region)
    ret['extra']['targetpool']['nodes'] = []
    for n in nodes:
        ret['extra']['targetpool']['nodes'].append(_expand_node(n))
    ret['extra']['targetpool']['healthchecks'] = []
    for hci in hc:
        ret['extra']['targetpool']['healthchecks'].append(hci.name)
    ret['extra']['targetpool']['region']['zones'] = []
    for z in zones:
        ret['extra']['targetpool']['region']['zones'].append(z.name)
    return ret


def show_instance(vm_name, call=None):
    '''
    Show the details of the existing instance.
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The show_instance action must be called with -a or --action.'
        )
    conn = get_conn()
    node = _expand_node(conn.ex_get_node(vm_name))
    __utils__['cloud.cache_node'](node, __active_provider_name__, __opts__)
    return node


def avail_sizes(conn=None):
    '''
    Return a dict of available instances sizes (a.k.a machine types) and
    convert them to something more serializable.
    '''
    if not conn:
        conn = get_conn()
    raw_sizes = conn.list_sizes('all')  # get *all* the machine types!
    sizes = []
    for size in raw_sizes:
        zone = size.extra['zone']
        size.extra['zone'] = {}
        size.extra['zone'].update(zone.__dict__)
        mtype = {}
        mtype.update(size.__dict__)
        sizes.append(mtype)
    return sizes


def avail_images(conn=None):
    '''
    Return a dict of all available VM images on the cloud provider with
    relevant data

    Note that for GCE, there are custom images within the project, but the
    generic images are in other projects.  This returns a dict of images in
    the project plus images in 'debian-cloud' and 'centos-cloud' (If there is
    overlap in names, the one in the current project is used.)
    '''
    if not conn:
        conn = get_conn()

    project_images = conn.list_images()
    debian_images = conn.list_images('debian-cloud')
    centos_images = conn.list_images('centos-cloud')

    all_images = debian_images + centos_images + project_images

    ret = {}
    for img in all_images:
        ret[img.name] = {}
        for attr in dir(img):
            if attr.startswith('_'):
                continue
            ret[img.name][attr] = getattr(img, attr)
    return ret


def __get_image(conn, vm_):
    '''
    The get_image for GCE allows partial name matching and returns a
    libcloud object.
    '''
    img = config.get_cloud_config_value(
        'image', vm_, __opts__, default='debian-7', search_global=False)
    return conn.ex_get_image(img)


def __get_location(conn, vm_):
    '''
    Need to override libcloud to find the zone.
    '''
    location = config.get_cloud_config_value(
        'location', vm_, __opts__)
    return conn.ex_get_zone(location)


def __get_size(conn, vm_):
    '''
    Need to override libcloud to find the machine type in the proper zone.
    '''
    size = config.get_cloud_config_value(
        'size', vm_, __opts__, default='n1-standard-1', search_global=False)
    return conn.ex_get_size(size, __get_location(conn, vm_))


def __get_tags(vm_):
    '''
    Get configured tags.
    '''
    t = config.get_cloud_config_value(
        'tags', vm_, __opts__,
        default='[]', search_global=False)
    # Consider warning the user that the tags in the cloud profile
    # could not be interpreted, bad formatting?
    try:
        tags = literal_eval(t)
    except Exception:  # pylint: disable=W0703
        tags = None
    if not tags or not isinstance(tags, list):
        tags = None
    return tags


def __get_metadata(vm_):
    '''
    Get configured metadata and add 'salt-cloud-profile'.
    '''
    md = config.get_cloud_config_value(
        'metadata', vm_, __opts__,
        default='{}', search_global=False)
    # Consider warning the user that the metadata in the cloud profile
    # could not be interpreted, bad formatting?
    try:
        metadata = literal_eval(md)
    except Exception:  # pylint: disable=W0703
        metadata = None
    if not metadata or not isinstance(metadata, dict):
        metadata = {'items': [{
            'key': 'salt-cloud-profile',
            'value': vm_['profile']
        }]}
    else:
        metadata['salt-cloud-profile'] = vm_['profile']
        items = []
        for k, v in six.iteritems(metadata):
            items.append({'key': k, 'value': v})
        metadata = {'items': items}
    return metadata


def __get_host(node, vm_):
    '''
    Return public IP, private IP, or hostname for the libcloud 'node' object
    '''
    if __get_ssh_interface(vm_) == 'private_ips' or vm_['external_ip'] is None:
        ip_address = node.private_ips[0]
        log.info('Salt node data. Private_ip: {0}'.format(ip_address))
    else:
        ip_address = node.public_ips[0]
        log.info('Salt node data. Public_ip: {0}'.format(ip_address))

    if len(ip_address) > 0:
        return ip_address

    return node.name


def __get_network(conn, vm_):
    '''
    Return a GCE libcloud network object with matching name
    '''
    network = config.get_cloud_config_value(
        'network', vm_, __opts__,
        default='default', search_global=False)
    return conn.ex_get_network(network)


def __get_ssh_interface(vm_):
    '''
    Return the ssh_interface type to connect to. Either 'public_ips' (default)
    or 'private_ips'.
    '''
    return config.get_cloud_config_value(
        'ssh_interface', vm_, __opts__, default='public_ips',
        search_global=False
    )


def __create_orget_address(conn, name, region):
    '''
    Reuse or create a static IP address.
    Returns a native GCEAddress construct to use with libcloud.
    '''
    try:
        addy = conn.ex_get_address(name, region)
    except ResourceNotFoundError:  # pylint: disable=W0703
        addr_kwargs = {
            'name': name,
            'region': region
        }
        new_addy = create_address(addr_kwargs, "function")
        addy = conn.ex_get_address(new_addy['name'], new_addy['region'])

    return addy


def _parse_allow(allow):
    '''
    Convert firewall rule allowed user-string to specified REST API format.
    '''
    # input=> tcp:53,tcp:80,tcp:443,icmp,tcp:4201,udp:53
    # output<= [
    #     {"IPProtocol": "tcp", "ports": ["53","80","443","4201"]},
    #     {"IPProtocol": "icmp"},
    #     {"IPProtocol": "udp", "ports": ["53"]},
    # ]
    seen_protos = {}
    allow_dict = []
    protocols = allow.split(',')
    for p in protocols:
        pairs = p.split(':')
        if pairs[0].lower() not in ['tcp', 'udp', 'icmp']:
            raise SaltCloudSystemExit(
                'Unsupported protocol {0}. Must be tcp, udp, or icmp.'.format(
                    pairs[0]
                )
            )
        if len(pairs) == 1 or pairs[0].lower() == 'icmp':
            seen_protos[pairs[0]] = []
        else:
            if pairs[0] not in seen_protos:
                seen_protos[pairs[0]] = [pairs[1]]
            else:
                seen_protos[pairs[0]].append(pairs[1])
    for k in seen_protos:
        d = {'IPProtocol': k}
        if len(seen_protos[k]) > 0:
            d['ports'] = seen_protos[k]
        allow_dict.append(d)
    log.debug("firewall allowed protocols/ports: {0}".format(allow_dict))
    return allow_dict


def __get_ssh_credentials(vm_):
    '''
    Get configured SSH credentials.
    '''
    ssh_user = config.get_cloud_config_value(
        'ssh_username', vm_, __opts__, default=os.getenv('USER'))
    ssh_key = config.get_cloud_config_value(
        'ssh_keyfile', vm_, __opts__,
        default=os.path.expanduser('~/.ssh/google_compute_engine'))
    return ssh_user, ssh_key


def create_network(kwargs=None, call=None):
    '''
    Create a GCE network.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f create_network gce name=mynet cidr=10.10.10.0/24
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The create_network function must be called with -f or --function.'
        )

    if not kwargs or 'name' not in kwargs:
        log.error(
            'A name must be specified when creating a network.'
        )
        return False
    if 'cidr' not in kwargs:
        log.error(
            'A network CIDR range must be specified when creating a network.'
        )
        return

    name = kwargs['name']
    cidr = kwargs['cidr']
    conn = get_conn()

    __utils__['cloud.fire_event'](
        'event',
        'create network',
        'salt/cloud/net/creating',
        {
            'name': name,
            'cidr': cidr,
        },
        transport=__opts__['transport']
    )

    network = conn.ex_create_network(name, cidr)

    __utils__['cloud.fire_event'](
        'event',
        'created network',
        'salt/cloud/net/created',
        {
            'name': name,
            'cidr': cidr,
        },
        transport=__opts__['transport']
    )
    return _expand_item(network)


def delete_network(kwargs=None, call=None):
    '''
    Permanently delete a network.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f delete_network gce name=mynet
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The delete_network function must be called with -f or --function.'
        )

    if not kwargs or 'name' not in kwargs:
        log.error(
            'A name must be specified when deleting a network.'
        )
        return False

    name = kwargs['name']
    conn = get_conn()

    __utils__['cloud.fire_event'](
        'event',
        'delete network',
        'salt/cloud/net/deleting',
        {
            'name': name,
        },
        transport=__opts__['transport']
    )

    try:
        result = conn.ex_destroy_network(
            conn.ex_get_network(name)
        )
    except ResourceNotFoundError as exc:
        log.error(
            'Nework {0} could not be found.\n'
            'The following exception was thrown by libcloud:\n{1}'.format(
                name, exc),
            exc_info_on_loglevel=logging.DEBUG
        )
        return False

    __utils__['cloud.fire_event'](
        'event',
        'deleted network',
        'salt/cloud/net/deleted',
        {
            'name': name,
        },
        transport=__opts__['transport']
    )
    return result


def show_network(kwargs=None, call=None):
    '''
    Show the details of an existing network.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f show_network gce name=mynet
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The show_network function must be called with -f or --function.'
        )
    if not kwargs or 'name' not in kwargs:
        log.error(
            'Must specify name of network.'
        )
        return False

    conn = get_conn()
    return _expand_item(conn.ex_get_network(kwargs['name']))


def create_fwrule(kwargs=None, call=None):
    '''
    Create a GCE firewall rule. The 'default' network is used if not specified.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f create_fwrule gce name=allow-http allow=tcp:80
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The create_fwrule function must be called with -f or --function.'
        )

    if not kwargs or 'name' not in kwargs:
        log.error(
            'A name must be specified when creating a firewall rule.'
        )
        return False
    if 'allow' not in kwargs:
        log.error(
            'Must use "allow" to specify allowed protocols/ports.'
        )
        return False

    name = kwargs['name']
    network_name = kwargs.get('network', 'default')
    allow = _parse_allow(kwargs['allow'])
    src_range = kwargs.get('src_range', '0.0.0.0/0')
    src_tags = kwargs.get('src_tags', None)
    dst_tags = kwargs.get('dst_tags', None)

    if src_range:
        src_range = src_range.split(',')
    if src_tags:
        src_tags = src_tags.split(',')
    if dst_tags:
        dst_tags = dst_tags.split(',')
    conn = get_conn()

    __utils__['cloud.fire_event'](
        'event',
        'create firewall',
        'salt/cloud/firewall/creating',
        {
            'name': name,
            'network': network_name,
            'allow': kwargs['allow'],
        },
        transport=__opts__['transport']
    )

    fwrule = conn.ex_create_firewall(
        name, allow,
        network=network_name,
        source_ranges=src_range,
        source_tags=src_tags,
        target_tags=dst_tags
    )

    __utils__['cloud.fire_event'](
        'event',
        'created firewall',
        'salt/cloud/firewall/created',
        {
            'name': name,
            'network': network_name,
            'allow': kwargs['allow'],
        },
        transport=__opts__['transport']
    )
    return _expand_item(fwrule)


def delete_fwrule(kwargs=None, call=None):
    '''
    Permanently delete a firewall rule.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f delete_fwrule gce name=allow-http
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The delete_fwrule function must be called with -f or --function.'
        )

    if not kwargs or 'name' not in kwargs:
        log.error(
            'A name must be specified when deleting a firewall rule.'
        )
        return False

    name = kwargs['name']
    conn = get_conn()

    __utils__['cloud.fire_event'](
        'event',
        'delete firewall',
        'salt/cloud/firewall/deleting',
        {
            'name': name,
        },
        transport=__opts__['transport']
    )

    try:
        result = conn.ex_destroy_firewall(
            conn.ex_get_firewall(name)
        )
    except ResourceNotFoundError as exc:
        log.error(
            'Rule {0} could not be found.\n'
            'The following exception was thrown by libcloud:\n{1}'.format(
                name, exc),
            exc_info_on_loglevel=logging.DEBUG
        )
        return False

    __utils__['cloud.fire_event'](
        'event',
        'deleted firewall',
        'salt/cloud/firewall/deleted',
        {
            'name': name,
        },
        transport=__opts__['transport']
    )
    return result


def show_fwrule(kwargs=None, call=None):
    '''
    Show the details of an existing firewall rule.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f show_fwrule gce name=allow-http
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The show_fwrule function must be called with -f or --function.'
        )
    if not kwargs or 'name' not in kwargs:
        log.error(
            'Must specify name of network.'
        )
        return False

    conn = get_conn()
    return _expand_item(conn.ex_get_firewall(kwargs['name']))


def create_hc(kwargs=None, call=None):
    '''
    Create an HTTP health check configuration.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f create_hc gce name=hc path=/healthy port=80
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The create_hc function must be called with -f or --function.'
        )

    if not kwargs or 'name' not in kwargs:
        log.error(
            'A name must be specified when creating a health check.'
        )
        return False

    name = kwargs['name']
    host = kwargs.get('host', None)
    path = kwargs.get('path', None)
    port = kwargs.get('port', None)
    interval = kwargs.get('interval', None)
    timeout = kwargs.get('timeout', None)
    unhealthy_threshold = kwargs.get('unhealthy_threshold', None)
    healthy_threshold = kwargs.get('healthy_threshold', None)

    conn = get_conn()

    __utils__['cloud.fire_event'](
        'event',
        'create health_check',
        'salt/cloud/healthcheck/creating',
        {
            'name': name,
            'host': host,
            'path': path,
            'port': port,
            'interval': interval,
            'timeout': timeout,
            'unhealthy_threshold': unhealthy_threshold,
            'healthy_threshold': healthy_threshold,
        },
        transport=__opts__['transport']
    )

    hc = conn.ex_create_healthcheck(
        name, host=host, path=path, port=port, interval=interval,
        timeout=timeout, unhealthy_threshold=unhealthy_threshold,
        healthy_threshold=healthy_threshold
    )

    __utils__['cloud.fire_event'](
        'event',
        'created health_check',
        'salt/cloud/healthcheck/created',
        {
            'name': name,
            'host': host,
            'path': path,
            'port': port,
            'interval': interval,
            'timeout': timeout,
            'unhealthy_threshold': unhealthy_threshold,
            'healthy_threshold': healthy_threshold,
        },
        transport=__opts__['transport']
    )
    return _expand_item(hc)


def delete_hc(kwargs=None, call=None):
    '''
    Permanently delete a health check.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f delete_hc gce name=hc
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The delete_hc function must be called with -f or --function.'
        )

    if not kwargs or 'name' not in kwargs:
        log.error(
            'A name must be specified when deleting a health check.'
        )
        return False

    name = kwargs['name']
    conn = get_conn()

    __utils__['cloud.fire_event'](
        'event',
        'delete health_check',
        'salt/cloud/healthcheck/deleting',
        {
            'name': name,
        },
        transport=__opts__['transport']
    )

    try:
        result = conn.ex_destroy_healthcheck(
            conn.ex_get_healthcheck(name)
        )
    except ResourceNotFoundError as exc:
        log.error(
            'Health check {0} could not be found.\n'
            'The following exception was thrown by libcloud:\n{1}'.format(
                name, exc),
            exc_info_on_loglevel=logging.DEBUG
        )
        return False

    __utils__['cloud.fire_event'](
        'event',
        'deleted health_check',
        'salt/cloud/healthcheck/deleted',
        {
            'name': name,
        },
        transport=__opts__['transport']
    )
    return result


def show_hc(kwargs=None, call=None):
    '''
    Show the details of an existing health check.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f show_hc gce name=hc
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The show_hc function must be called with -f or --function.'
        )
    if not kwargs or 'name' not in kwargs:
        log.error(
            'Must specify name of health check.'
        )
        return False

    conn = get_conn()
    return _expand_item(conn.ex_get_healthcheck(kwargs['name']))


def create_address(kwargs=None, call=None):
    '''
    Create a static address in a region.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f create_address gce name=my-ip region=us-central1 address=IP
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The create_address function must be called with -f or --function.'
        )

    if not kwargs or 'name' not in kwargs:
        log.error(
            'A name must be specified when creating an address.'
        )
        return False
    if 'region' not in kwargs:
        log.error(
            'A region must be specified for the address.'
        )
        return False

    name = kwargs['name']
    ex_region = kwargs['region']
    ex_address = kwargs.get("address", None)

    conn = get_conn()

    __utils__['cloud.fire_event'](
        'event',
        'create address',
        'salt/cloud/address/creating',
        kwargs,
        transport=__opts__['transport']
    )

    addy = conn.ex_create_address(name, ex_region, ex_address)

    __utils__['cloud.fire_event'](
        'event',
        'created address',
        'salt/cloud/address/created',
        kwargs,
        transport=__opts__['transport']
    )

    log.info('Created GCE Address '+name)

    return _expand_address(addy)


def delete_address(kwargs=None, call=None):
    '''
    Permanently delete a static address.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f delete_address gce name=my-ip
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The delete_address function must be called with -f or --function.'
        )

    if not kwargs or 'name' not in kwargs:
        log.error(
            'A name must be specified when deleting an address.'
        )
        return False

    if not kwargs or 'region' not in kwargs:
        log.error(
            'A region must be specified when deleting an address.'
        )
        return False

    name = kwargs['name']
    ex_region = kwargs['region']

    conn = get_conn()

    __utils__['cloud.fire_event'](
        'event',
        'delete address',
        'salt/cloud/address/deleting',
        {
            'name': name,
        },
        transport=__opts__['transport']
    )

    try:
        result = conn.ex_destroy_address(
            conn.ex_get_address(name, ex_region)
        )
    except ResourceNotFoundError as exc:
        log.error(
            'Address {0} could not be found (region {1})\n'
            'The following exception was thrown by libcloud:\n{2}'.format(
                name, ex_region, exc),
            exc_info_on_loglevel=logging.DEBUG
        )
        return False

    __utils__['cloud.fire_event'](
        'event',
        'deleted address',
        'salt/cloud/address/deleted',
        {
            'name': name,
        },
        transport=__opts__['transport']
    )

    log.info('Deleted GCE Address ' + name)

    return result


def show_address(kwargs=None, call=None):
    '''
    Show the details of an existing static address.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f show_address gce name=mysnapshot region=us-central1
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The show_snapshot function must be called with -f or --function.'
        )
    if not kwargs or 'name' not in kwargs:
        log.error(
            'Must specify name.'
        )
        return False

    if not kwargs or 'region' not in kwargs:
        log.error(
            'Must specify region.'
        )
        return False

    conn = get_conn()
    return _expand_address(conn.ex_get_address(kwargs['name'], kwargs['region']))


def create_lb(kwargs=None, call=None):
    '''
    Create a load-balancer configuration.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f create_lb gce name=lb region=us-central1 ports=80
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The create_lb function must be called with -f or --function.'
        )

    if not kwargs or 'name' not in kwargs:
        log.error(
            'A name must be specified when creating a health check.'
        )
        return False
    if 'ports' not in kwargs:
        log.error(
            'A port or port-range must be specified for the load-balancer.'
        )
        return False
    if 'region' not in kwargs:
        log.error(
            'A region must be specified for the load-balancer.'
        )
        return False
    if 'members' not in kwargs:
        log.error(
            'A comma-separated list of members must be specified.'
        )
        return False

    name = kwargs['name']
    ports = kwargs['ports']
    ex_region = kwargs['region']
    members = kwargs.get('members').split(',')

    protocol = kwargs.get('protocol', 'tcp')
    algorithm = kwargs.get('algorithm', None)
    ex_healthchecks = kwargs.get('healthchecks', None)

    # pylint: disable=W0511

    conn = get_conn()
    lb_conn = get_lb_conn(conn)

    ex_address = kwargs.get('address', None)
    if ex_address is not None:
        ex_address = __create_orget_address(conn, ex_address, ex_region)

    if ex_healthchecks:
        ex_healthchecks = ex_healthchecks.split(',')

    __utils__['cloud.fire_event'](
        'event',
        'create load_balancer',
        'salt/cloud/loadbalancer/creating',
        kwargs,
        transport=__opts__['transport']
    )

    lb = lb_conn.create_balancer(
        name, ports, protocol, algorithm, members,
        ex_region=ex_region, ex_healthchecks=ex_healthchecks,
        ex_address=ex_address
    )

    __utils__['cloud.fire_event'](
        'event',
        'created load_balancer',
        'salt/cloud/loadbalancer/created',
        kwargs,
        transport=__opts__['transport']
    )
    return _expand_balancer(lb)


def delete_lb(kwargs=None, call=None):
    '''
    Permanently delete a load-balancer.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f delete_lb gce name=lb
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The delete_hc function must be called with -f or --function.'
        )

    if not kwargs or 'name' not in kwargs:
        log.error(
            'A name must be specified when deleting a health check.'
        )
        return False

    name = kwargs['name']
    lb_conn = get_lb_conn(get_conn())

    __utils__['cloud.fire_event'](
        'event',
        'delete load_balancer',
        'salt/cloud/loadbalancer/deleting',
        {
            'name': name,
        },
        transport=__opts__['transport']
    )

    try:
        result = lb_conn.destroy_balancer(
            lb_conn.get_balancer(name)
        )
    except ResourceNotFoundError as exc:
        log.error(
            'Load balancer {0} could not be found.\n'
            'The following exception was thrown by libcloud:\n{1}'.format(
                name, exc),
            exc_info_on_loglevel=logging.DEBUG
        )
        return False

    __utils__['cloud.fire_event'](
        'event',
        'deleted load_balancer',
        'salt/cloud/loadbalancer/deleted',
        {
            'name': name,
        },
        transport=__opts__['transport']
    )
    return result


def show_lb(kwargs=None, call=None):
    '''
    Show the details of an existing load-balancer.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f show_lb gce name=lb
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The show_lb function must be called with -f or --function.'
        )
    if not kwargs or 'name' not in kwargs:
        log.error(
            'Must specify name of load-balancer.'
        )
        return False

    lb_conn = get_lb_conn(get_conn())
    return _expand_balancer(lb_conn.get_balancer(kwargs['name']))


def attach_lb(kwargs=None, call=None):
    '''
    Add an existing node/member to an existing load-balancer configuration.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f attach_lb gce name=lb member=myinstance
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The attach_lb function must be called with -f or --function.'
        )

    if not kwargs or 'name' not in kwargs:
        log.error(
            'A load-balancer name must be specified.'
        )
        return False
    if 'member' not in kwargs:
        log.error(
            'A node name name must be specified.'
        )
        return False

    conn = get_conn()
    node = conn.ex_get_node(kwargs['member'])

    lb_conn = get_lb_conn(conn)
    lb = lb_conn.get_balancer(kwargs['name'])

    __utils__['cloud.fire_event'](
        'event',
        'attach load_balancer',
        'salt/cloud/loadbalancer/attaching',
        kwargs,
        transport=__opts__['transport']
    )

    result = lb_conn.balancer_attach_compute_node(lb, node)

    __utils__['cloud.fire_event'](
        'event',
        'attached load_balancer',
        'salt/cloud/loadbalancer/attached',
        kwargs,
        transport=__opts__['transport']
    )
    return _expand_item(result)


def detach_lb(kwargs=None, call=None):
    '''
    Remove an existing node/member from an existing load-balancer configuration.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f detach_lb gce name=lb member=myinstance
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The detach_lb function must be called with -f or --function.'
        )

    if not kwargs or 'name' not in kwargs:
        log.error(
            'A load-balancer name must be specified.'
        )
        return False
    if 'member' not in kwargs:
        log.error(
            'A node name name must be specified.'
        )
        return False

    conn = get_conn()
    lb_conn = get_lb_conn(conn)
    lb = lb_conn.get_balancer(kwargs['name'])

    member_list = lb_conn.balancer_list_members(lb)
    remove_member = None
    for member in member_list:
        if member.id == kwargs['member']:
            remove_member = member
            break

    if not remove_member:
        log.error(
            'The specified member {0} was not a member of LB {1}.'.format(
                kwargs['member'], kwargs['name']
            )
        )
        return False

    __utils__['cloud.fire_event'](
        'event',
        'detach load_balancer',
        'salt/cloud/loadbalancer/detaching',
        kwargs,
        transport=__opts__['transport']
    )

    result = lb_conn.balancer_detach_member(lb, remove_member)

    __utils__['cloud.fire_event'](
        'event',
        'detached load_balancer',
        'salt/cloud/loadbalancer/detached',
        kwargs,
        transport=__opts__['transport']
    )
    return result


def delete_snapshot(kwargs=None, call=None):
    '''
    Permanently delete a disk snapshot.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f delete_snapshot gce name=disk-snap-1
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The delete_snapshot function must be called with -f or --function.'
        )

    if not kwargs or 'name' not in kwargs:
        log.error(
            'A name must be specified when deleting a snapshot.'
        )
        return False

    name = kwargs['name']
    conn = get_conn()

    __utils__['cloud.fire_event'](
        'event',
        'delete snapshot',
        'salt/cloud/snapshot/deleting',
        {
            'name': name,
        },
        transport=__opts__['transport']
    )

    try:
        result = conn.destroy_volume_snapshot(
            conn.ex_get_snapshot(name)
        )
    except ResourceNotFoundError as exc:
        log.error(
            'Snapshot {0} could not be found.\n'
            'The following exception was thrown by libcloud:\n{1}'.format(
                name, exc),
            exc_info_on_loglevel=logging.DEBUG
        )
        return False

    __utils__['cloud.fire_event'](
        'event',
        'deleted snapshot',
        'salt/cloud/snapshot/deleted',
        {
            'name': name,
        },
        transport=__opts__['transport']
    )
    return result


def delete_disk(kwargs=None, call=None):
    '''
    Permanently delete a persistent disk.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f delete_disk gce disk_name=pd
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The delete_disk function must be called with -f or --function.'
        )

    if not kwargs or 'disk_name' not in kwargs:
        log.error(
            'A disk_name must be specified when deleting a disk.'
        )
        return False

    conn = get_conn()

    disk = conn.ex_get_volume(kwargs.get('disk_name'))

    __utils__['cloud.fire_event'](
        'event',
        'delete disk',
        'salt/cloud/disk/deleting',
        {
            'name': disk.name,
            'location': disk.extra['zone'].name,
            'size': disk.size,
        },
        transport=__opts__['transport']
    )

    try:
        result = conn.destroy_volume(disk)
    except ResourceInUseError as exc:
        log.error(
            'Disk {0} is in use and must be detached before deleting.\n'
            'The following exception was thrown by libcloud:\n{1}'.format(
                disk.name, exc),
            exc_info_on_loglevel=logging.DEBUG
        )
        return False

    __utils__['cloud.fire_event'](
        'event',
        'deleted disk',
        'salt/cloud/disk/deleted',
        {
            'name': disk.name,
            'location': disk.extra['zone'].name,
            'size': disk.size,
        },
        transport=__opts__['transport']
    )
    return result


def create_disk(kwargs=None, call=None):

    '''
    Create a new persistent disk. Must specify `disk_name` and `location`.
    Can also specify an `image` or `snapshot` but if neither of those are
    specified, a `size` (in GB) is required.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f create_disk gce disk_name=pd size=300 location=us-central1-b
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The create_disk function must be called with -f or --function.'
        )

    if kwargs is None:
        kwargs = {}

    name = kwargs.get('disk_name', None)
    image = kwargs.get('image', None)
    location = kwargs.get('location', None)
    size = kwargs.get('size', None)
    snapshot = kwargs.get('snapshot', None)

    if location is None:
        log.error(
            'A location (zone) must be specified when creating a disk.'
        )
        return False

    if name is None:
        log.error(
            'A disk_name must be specified when creating a disk.'
        )
        return False

    if 'size' is None and 'image' is None and 'snapshot' is None:
        log.error(
            'Must specify image, snapshot, or size.'
        )
        return False

    conn = get_conn()

    location = conn.ex_get_zone(kwargs['location'])
    use_existing = True

    __utils__['cloud.fire_event'](
        'event',
        'create disk',
        'salt/cloud/disk/creating',
        {
            'name': name,
            'location': location.name,
            'image': image,
            'snapshot': snapshot,
        },
        transport=__opts__['transport']
    )

    disk = conn.create_volume(
        size, name, location, snapshot, image, use_existing
    )

    __utils__['cloud.fire_event'](
        'event',
        'created disk',
        'salt/cloud/disk/created',
        {
            'name': name,
            'location': location.name,
            'image': image,
            'snapshot': snapshot,
        },
        transport=__opts__['transport']
    )
    return _expand_disk(disk)


def create_snapshot(kwargs=None, call=None):
    '''
    Create a new disk snapshot. Must specify `name` and  `disk_name`.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f create_snapshot gce name=snap1 disk_name=pd
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The create_snapshot function must be called with -f or --function.'
        )

    if not kwargs or 'name' not in kwargs:
        log.error(
            'A name must be specified when creating a snapshot.'
        )
        return False

    if 'disk_name' not in kwargs:
        log.error(
            'A disk_name must be specified when creating a snapshot.'
        )
        return False

    conn = get_conn()

    name = kwargs.get('name')
    disk_name = kwargs.get('disk_name')

    try:
        disk = conn.ex_get_volume(disk_name)
    except ResourceNotFoundError as exc:
        log.error(
            'Disk {0} could not be found.\n'
            'The following exception was thrown by libcloud:\n{1}'.format(
                disk_name, exc),
            exc_info_on_loglevel=logging.DEBUG
        )
        return False

    __utils__['cloud.fire_event'](
        'event',
        'create snapshot',
        'salt/cloud/snapshot/creating',
        {
            'name': name,
            'disk_name': disk_name,
        },
        transport=__opts__['transport']
    )

    snapshot = conn.create_volume_snapshot(disk, name)

    __utils__['cloud.fire_event'](
        'event',
        'created snapshot',
        'salt/cloud/snapshot/created',
        {
            'name': name,
            'disk_name': disk_name,
        },
        transport=__opts__['transport']
    )
    return _expand_item(snapshot)


def show_disk(name=None, kwargs=None, call=None):  # pylint: disable=W0613
    '''
    Show the details of an existing disk.

    CLI Example:

    .. code-block:: bash

        salt-cloud -a show_disk myinstance disk_name=mydisk
        salt-cloud -f show_disk gce disk_name=mydisk
    '''
    if not kwargs or 'disk_name' not in kwargs:
        log.error(
            'Must specify disk_name.'
        )
        return False

    conn = get_conn()
    return _expand_disk(conn.ex_get_volume(kwargs['disk_name']))


def show_snapshot(kwargs=None, call=None):
    '''
    Show the details of an existing snapshot.

    CLI Example:

    .. code-block:: bash

        salt-cloud -f show_snapshot gce name=mysnapshot
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The show_snapshot function must be called with -f or --function.'
        )
    if not kwargs or 'name' not in kwargs:
        log.error(
            'Must specify name.'
        )
        return False

    conn = get_conn()
    return _expand_item(conn.ex_get_snapshot(kwargs['name']))


def detach_disk(name=None, kwargs=None, call=None):
    '''
    Detach a disk from an instance.

    CLI Example:

    .. code-block:: bash

        salt-cloud -a detach_disk myinstance disk_name=mydisk
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The detach_Disk action must be called with -a or --action.'
        )

    if not name:
        log.error(
            'Must specify an instance name.'
        )
        return False
    if not kwargs or 'disk_name' not in kwargs:
        log.error(
            'Must specify a disk_name to detach.'
        )
        return False

    node_name = name
    disk_name = kwargs['disk_name']

    conn = get_conn()
    node = conn.ex_get_node(node_name)
    disk = conn.ex_get_volume(disk_name)

    __utils__['cloud.fire_event'](
        'event',
        'detach disk',
        'salt/cloud/disk/detaching',
        {
            'name': node_name,
            'disk_name': disk_name,
        },
        transport=__opts__['transport']
    )

    result = conn.detach_volume(disk, node)

    __utils__['cloud.fire_event'](
        'event',
        'detached disk',
        'salt/cloud/disk/detached',
        {
            'name': node_name,
            'disk_name': disk_name,
        },
        transport=__opts__['transport']
    )
    return result


def attach_disk(name=None, kwargs=None, call=None):
    '''
    Attach an existing disk to an existing instance.

    CLI Example:

    .. code-block:: bash

        salt-cloud -a attach_disk myinstance disk_name=mydisk mode=READ_WRITE
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The attach_disk action must be called with -a or --action.'
        )

    if not name:
        log.error(
            'Must specify an instance name.'
        )
        return False
    if not kwargs or 'disk_name' not in kwargs:
        log.error(
            'Must specify a disk_name to attach.'
        )
        return False

    node_name = name
    disk_name = kwargs['disk_name']
    mode = kwargs.get('mode', 'READ_WRITE').upper()
    boot = kwargs.get('boot', False)
    if boot and boot.lower() in ['true', 'yes', 'enabled']:
        boot = True
    else:
        boot = False

    if mode not in ['READ_WRITE', 'READ_ONLY']:
        log.error(
            'Mode must be either READ_ONLY or (default) READ_WRITE.'
        )
        return False

    conn = get_conn()
    node = conn.ex_get_node(node_name)
    disk = conn.ex_get_volume(disk_name)

    __utils__['cloud.fire_event'](
        'event',
        'attach disk',
        'salt/cloud/disk/attaching',
        {
            'name': node_name,
            'disk_name': disk_name,
            'mode': mode,
            'boot': boot,
        },
        transport=__opts__['transport']
    )

    result = conn.attach_volume(node, disk, ex_mode=mode, ex_boot=boot)

    __utils__['cloud.fire_event'](
        'event',
        'attached disk',
        'salt/cloud/disk/attached',
        {
            'name': node_name,
            'disk_name': disk_name,
            'mode': mode,
            'boot': boot,
        },
        transport=__opts__['transport']
    )
    return result


def reboot(vm_name, call=None):
    '''
    Call GCE 'reset' on the instance.

    CLI Example:

    .. code-block:: bash

        salt-cloud -a reboot myinstance
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The reboot action must be called with -a or --action.'
        )
    conn = get_conn()
    return conn.reboot_node(
        conn.ex_get_node(vm_name)
    )


def destroy(vm_name, call=None):
    '''
    Call 'destroy' on the instance.  Can be called with "-a destroy" or -d

    CLI Example:

    .. code-block:: bash

        salt-cloud -a destroy myinstance1 myinstance2 ...
        salt-cloud -d myinstance1 myinstance2 ...
    '''
    if call and call != 'action':
        raise SaltCloudSystemExit(
            'The destroy action must be called with -d or "-a destroy".'
        )

    conn = get_conn()

    try:
        node = conn.ex_get_node(vm_name)
    except Exception as exc:  # pylint: disable=W0703
        log.error(
            'Could not locate instance {0}\n\n'
            'The following exception was thrown by libcloud when trying to '
            'run the initial deployment: \n{1}'.format(
                vm_name, exc
            ),
            exc_info_on_loglevel=logging.DEBUG
        )
        raise SaltCloudSystemExit(
            'Could not find instance {0}.'.format(vm_name)
        )

    __utils__['cloud.fire_event'](
        'event',
        'delete instance',
        'salt/cloud/{0}/deleting'.format(vm_name),
        {'name': vm_name},
        transport=__opts__['transport']
    )

    # Use the instance metadata to see if its salt cloud profile was
    # preserved during instance create.  If so, use the profile value
    # to see if the 'delete_boot_pd' value is set to delete the disk
    # along with the instance.
    profile = None
    if node.extra['metadata'] and 'items' in node.extra['metadata']:
        for md in node.extra['metadata']['items']:
            if md['key'] == 'salt-cloud-profile':
                profile = md['value']
    vm_ = get_configured_provider()
    delete_boot_pd = False

    if profile and profile in vm_['profiles'] and 'delete_boot_pd' in vm_['profiles'][profile]:
        delete_boot_pd = vm_['profiles'][profile]['delete_boot_pd']

    try:
        inst_deleted = conn.destroy_node(node)
    except Exception as exc:  # pylint: disable=W0703
        log.error(
            'Could not destroy instance {0}\n\n'
            'The following exception was thrown by libcloud when trying to '
            'run the initial deployment: \n{1}'.format(
                vm_name, exc
            ),
            exc_info_on_loglevel=logging.DEBUG
        )
        raise SaltCloudSystemExit(
            'Could not destroy instance {0}.'.format(vm_name)
        )
    __utils__['cloud.fire_event'](
        'event',
        'delete instance',
        'salt/cloud/{0}/deleted'.format(vm_name),
        {'name': vm_name},
        transport=__opts__['transport']
    )

    if delete_boot_pd:
        log.info(
            'delete_boot_pd is enabled for the instance profile, '
            'attempting to delete disk'
            )
        __utils__['cloud.fire_event'](
            'event',
            'delete disk',
            'salt/cloud/disk/deleting',
            {'name': vm_name},
            transport=__opts__['transport']
        )
        try:
            conn.destroy_volume(conn.ex_get_volume(vm_name))
        except Exception as exc:  # pylint: disable=W0703
            # Note that we don't raise a SaltCloudSystemExit here in order
            # to allow completion of instance deletion.  Just log the error
            # and keep going.
            log.error(
                'Could not destroy disk {0}\n\n'
                'The following exception was thrown by libcloud when trying '
                'to run the initial deployment: \n{1}'.format(
                    vm_name, exc
                ),
                exc_info_on_loglevel=logging.DEBUG
            )
        __utils__['cloud.fire_event'](
            'event',
            'deleted disk',
            'salt/cloud/disk/deleted',
            {'name': vm_name},
            transport=__opts__['transport']
        )

    if __opts__.get('update_cachedir', False) is True:
        __utils__['cloud.delete_minion_cachedir'](vm_name, __active_provider_name__.split(':')[0], __opts__)

    return inst_deleted


def create(vm_=None, call=None):
    '''
    Create a single GCE instance from a data dict.
    '''
    if call:
        raise SaltCloudSystemExit(
            'You cannot create an instance with -a or -f.'
        )

    if not GCE_VM_NAME_REGEX.match(vm_['name']):
        raise SaltCloudSystemExit(
            'VM names must start with a letter, only contain letters, numbers, or dashes '
            'and cannot end in a dash.'
        )

    try:
        # Check for required profile parameters before sending any API calls.
        if vm_['profile'] and config.is_profile_configured(__opts__,
                                                           __active_provider_name__ or 'gce',
                                                           vm_['profile'],
                                                           vm_=vm_) is False:
            return False
    except AttributeError:
        pass

    # Since using "provider: <provider-engine>" is deprecated, alias provider
    # to use driver: "driver: <provider-engine>"
    if 'provider' in vm_:
        vm_['driver'] = vm_.pop('provider')

    conn = get_conn()

    kwargs = {
        'name': vm_['name'],
        'size': __get_size(conn, vm_),
        'image': __get_image(conn, vm_),
        'location': __get_location(conn, vm_),
        'ex_network': __get_network(conn, vm_),
        'ex_tags': __get_tags(vm_),
        'ex_metadata': __get_metadata(vm_),
    }
    external_ip = config.get_cloud_config_value(
        'external_ip', vm_, __opts__, default='ephemeral'
    )

    if external_ip.lower() == 'ephemeral':
        external_ip = 'ephemeral'
    elif external_ip == 'None':
        external_ip = None
    else:
        region = '-'.join(kwargs['location'].name.split('-')[:2])
        external_ip = __create_orget_address(conn, external_ip, region)
    kwargs['external_ip'] = external_ip
    vm_['external_ip'] = external_ip

    if LIBCLOUD_VERSION_INFO > (0, 15, 1):

        kwargs.update({
            'ex_disk_type': config.get_cloud_config_value(
                'ex_disk_type', vm_, __opts__, default='pd-standard'),
            'ex_disk_auto_delete': config.get_cloud_config_value(
                'ex_disk_auto_delete', vm_, __opts__, default=True),
            'ex_disks_gce_struct': config.get_cloud_config_value(
                'ex_disks_gce_struct', vm_, __opts__, default=None),
            'ex_service_accounts': config.get_cloud_config_value(
                'ex_service_accounts', vm_, __opts__, default=None),
            'ex_can_ip_forward': config.get_cloud_config_value(
                'ip_forwarding', vm_, __opts__, default=False
            )
        })
        if kwargs.get('ex_disk_type') not in ('pd-standard', 'pd-ssd'):
            raise SaltCloudSystemExit(
                'The value of \'ex_disk_type\' needs to be one of: '
                '\'pd-standard\', \'pd-ssd\''
            )

    log.info('Creating GCE instance {0} in {1}'.format(vm_['name'],
        kwargs['location'].name)
    )
    log.debug('Create instance kwargs {0}'.format(str(kwargs)))

    __utils__['cloud.fire_event'](
        'event',
        'create instance',
        'salt/cloud/{0}/creating'.format(vm_['name']),
        {
            'name': vm_['name'],
            'profile': vm_['profile'],
            'provider': vm_['driver'],
        },
        transport=__opts__['transport']
    )

    try:
        node_data = conn.create_node(**kwargs)
    except Exception as exc:  # pylint: disable=W0703
        log.error(
            'Error creating {0} on GCE\n\n'
            'The following exception was thrown by libcloud when trying to '
            'run the initial deployment: \n{1}'.format(
                vm_['name'], exc
            ),
            exc_info_on_loglevel=logging.DEBUG
        )
        return False

    try:
        node_dict = show_instance(node_data['name'], 'action')
    except TypeError:
        # node_data is a libcloud Node which is unsubscriptable
        node_dict = show_instance(node_data.name, 'action')

    ssh_user, ssh_key = __get_ssh_credentials(vm_)
    vm_['ssh_host'] = __get_host(node_data, vm_)
    vm_['key_filename'] = ssh_key
    __utils__['cloud.bootstrap'](vm_, __opts__)

    log.info('Created Cloud VM {0[name]!r}'.format(vm_))
    log.trace(
        '{0[name]!r} VM creation details:\n{1}'.format(
            vm_, pprint.pformat(node_dict)
        )
    )

    __utils__['cloud.fire_event'](
        'event',
        'created instance',
        'salt/cloud/{0}/created'.format(vm_['name']),
        {
            'name': vm_['name'],
            'profile': vm_['profile'],
            'provider': vm_['driver'],
        },
        transport=__opts__['transport']
    )

    return node_dict


def update_pricing(kwargs=None, call=None):
    '''
    Download most recent pricing information from GCE and save locally

    CLI Examples:

    .. code-block:: bash

        salt-cloud -f update_pricing my-gce-config

    .. versionadded:: 2015.8.0
    '''
    url = 'https://cloudpricingcalculator.appspot.com/static/data/pricelist.json'
    price_json = http.query(url, decode=True, decode_type='json')

    outfile = os.path.join(
        __opts__['cachedir'], 'gce-pricing.p'
    )
    with salt.utils.fopen(outfile, 'w') as fho:
        msgpack.dump(price_json['dict'], fho)

    return True


def show_pricing(kwargs=None, call=None):
    '''
    Show pricing for a particular profile. This is only an estimate, based on
    unofficial pricing sources.

    .. versionadded:: 2015.8.0

    CLI Examples:

    .. code-block:: bash

        salt-cloud -f show_pricing my-gce-config profile=my-profile
    '''
    profile = __opts__['profiles'].get(kwargs['profile'], {})
    if not profile:
        return {'Error': 'The requested profile was not found'}

    # Make sure the profile belongs to Digital Ocean
    provider = profile.get('provider', '0:0')
    comps = provider.split(':')
    if len(comps) < 2 or comps[1] != 'gce':
        return {'Error': 'The requested profile does not belong to GCE'}

    comps = profile.get('location', 'us').split('-')
    region = comps[0]

    size = 'CP-COMPUTEENGINE-VMIMAGE-{0}'.format(profile['size'].upper())
    pricefile = os.path.join(
        __opts__['cachedir'], 'gce-pricing.p'
    )
    if not os.path.exists(pricefile):
        update_pricing()

    with salt.utils.fopen(pricefile, 'r') as fho:
        sizes = msgpack.load(fho)

    per_hour = float(sizes['gcp_price_list'][size][region])

    week1_discount = float(sizes['gcp_price_list']['sustained_use_tiers']['0.25'])
    week2_discount = float(sizes['gcp_price_list']['sustained_use_tiers']['0.50'])
    week3_discount = float(sizes['gcp_price_list']['sustained_use_tiers']['0.75'])
    week4_discount = float(sizes['gcp_price_list']['sustained_use_tiers']['1.0'])
    week1 = per_hour * (730/4) * week1_discount
    week2 = per_hour * (730/4) * week2_discount
    week3 = per_hour * (730/4) * week3_discount
    week4 = per_hour * (730/4) * week4_discount

    raw = sizes
    ret = {}

    ret['per_hour'] = per_hour
    ret['per_day'] = ret['per_hour'] * 24
    ret['per_week'] = ret['per_day'] * 7
    ret['per_month'] = week1 + week2 + week3 + week4
    ret['per_year'] = ret['per_month'] * 12

    if kwargs.get('raw', False):
        ret['_raw'] = raw

    return {profile['profile']: ret}
