# -*- coding: utf-8 -*-
'''
Work with virtual machines managed by libvirt

:depends: libvirt Python module
'''
# Special Thanks to Michael Dehann, many of the concepts, and a few structures
# of his in the virt func module have been used

# Import python libs
from __future__ import absolute_import
import os
import re
import sys
import shutil
import subprocess
import string  # pylint: disable=deprecated-module
import logging

# Import third party libs
import yaml
import jinja2
import jinja2.exceptions
from xml.dom import minidom
import salt.ext.six as six
from salt.ext.six.moves import StringIO as _StringIO  # pylint: disable=import-error
from xml.dom import minidom
try:
    import libvirt  # pylint: disable=import-error
    from libvirt import libvirtError
    HAS_LIBVIRT = True
except ImportError:
    HAS_LIBVIRT = False

# Import salt libs
import salt.utils
import salt.utils.files
import salt.utils.templates
import salt.utils.validate.net
from salt.exceptions import CommandExecutionError, SaltInvocationError

log = logging.getLogger(__name__)

# Set up template environment
JINJA = jinja2.Environment(
    loader=jinja2.FileSystemLoader(
        os.path.join(salt.utils.templates.TEMPLATE_DIRNAME, 'virt')
    )
)

VIRT_STATE_NAME_MAP = {0: 'running',
                       1: 'running',
                       2: 'running',
                       3: 'paused',
                       4: 'shutdown',
                       5: 'shutdown',
                       6: 'crashed'}

VIRT_DEFAULT_HYPER = 'kvm'


def __virtual__():
    if not HAS_LIBVIRT:
        return False
    return 'virt'


def __get_conn():
    '''
    Detects what type of dom this node is and attempts to connect to the
    correct hypervisor via libvirt.
    '''
    # This has only been tested on kvm and xen, it needs to be expanded to
    # support all vm layers supported by libvirt

    def __esxi_uri():
        '''
        Connect to an ESXi host with a configuration like so:

        .. code-block:: yaml

            libvirt:
              hypervisor: esxi
              connection: esx01

        The connection setting can either be an explicit libvirt URI,
        or a libvirt URI alias as in this example. No, it cannot be
        just a hostname.


        Example libvirt `/etc/libvirt/libvirt.conf`:

        .. code-block::

            uri_aliases = [
              "esx01=esx://10.1.1.101/?no_verify=1&auto_answer=1",
              "esx02=esx://10.1.1.102/?no_verify=1&auto_answer=1",
            ]

        Reference:

         - http://libvirt.org/drvesx.html#uriformat
         - http://libvirt.org/uri.html#URI_config
        '''
        connection = __salt__['config.get']('libvirt:connection', 'esx')
        return connection

    def __esxi_auth():
        '''
        We rely on that the credentials is provided to libvirt through
        its built in mechanisms.

        Example libvirt `/etc/libvirt/auth.conf`:

        .. code-block::

            [credentials-myvirt]
            username=user
            password=secret

            [auth-esx-10.1.1.101]
            credentials=myvirt

            [auth-esx-10.1.1.102]
            credentials=myvirt

        Reference:

          - http://libvirt.org/auth.html#Auth_client_config
        '''
        return [[libvirt.VIR_CRED_EXTERNAL], lambda: 0, None]

    if 'virt.connect' in __opts__:
        conn_str = __opts__['virt.connect']
    else:
        conn_str = 'qemu:///system'

    conn_func = {
        'esxi': [libvirt.openAuth, [__esxi_uri(),
                                    __esxi_auth(),
                                    0]],
        'qemu': [libvirt.open, [conn_str]],
    }

    hypervisor = __salt__['config.get']('libvirt:hypervisor', 'qemu')

    try:
        conn = conn_func[hypervisor][0](*conn_func[hypervisor][1])
    except Exception:
        raise CommandExecutionError(
            'Sorry, {0} failed to open a connection to the hypervisor '
            'software at {1}'.format(
                __grains__['fqdn'],
                conn_func[hypervisor][1][0]
            )
        )
    return conn


def _get_dom(vm_):
    '''
    Return a domain object for the named vm
    '''
    conn = __get_conn()
    if vm_ not in list_vms():
        raise CommandExecutionError('The specified vm is not present')
    return conn.lookupByName(vm_)


def _libvirt_creds():
    '''
    Returns the user and group that the disk images should be owned by
    '''
    g_cmd = 'grep ^\\s*group /etc/libvirt/qemu.conf'
    u_cmd = 'grep ^\\s*user /etc/libvirt/qemu.conf'
    try:
        stdout = subprocess.Popen(g_cmd,
                    shell=True,
                    stdout=subprocess.PIPE).communicate()[0]
        group = salt.utils.to_str(stdout).split('"')[1]
    except IndexError:
        group = 'root'
    try:
        stdout = subprocess.Popen(u_cmd,
                    shell=True,
                    stdout=subprocess.PIPE).communicate()[0]
        user = salt.utils.to_str(stdout).split('"')[1]
    except IndexError:
        user = 'root'
    return {'user': user, 'group': group}


def _get_migrate_command():
    '''
    Returns the command shared by the different migration types
    '''
    if __salt__['config.option']('virt.tunnel'):
        return ('virsh migrate --p2p --tunnelled --live --persistent '
                '--undefinesource ')
    return 'virsh migrate --live --persistent --undefinesource '


def _get_target(target, ssh):
    proto = 'qemu'
    if ssh:
        proto += '+ssh'
    return ' {0}://{1}/{2}'.format(proto, target, 'system')


def _gen_xml(name,
             cpu,
             mem,
             diskp,
             nicp,
             hypervisor,
             **kwargs):
    '''
    Generate the XML string to define a libvirt VM
    '''
    hypervisor = 'vmware' if hypervisor == 'esxi' else hypervisor
    mem = mem * 1024  # MB
    context = {
        'hypervisor': hypervisor,
        'name': name,
        'cpu': str(cpu),
        'mem': str(mem),
    }
    if hypervisor in ['qemu', 'kvm']:
        context['controller_model'] = False
    elif hypervisor in ['esxi', 'vmware']:
        # TODO: make bus and model parameterized, this works for 64-bit Linux
        context['controller_model'] = 'lsilogic'

    if 'boot_dev' in kwargs:
        context['boot_dev'] = []
        for dev in kwargs['boot_dev'].split():
            context['boot_dev'].append(dev)
    else:
        context['boot_dev'] = ['hd']

    if 'serial_type' in kwargs:
        context['serial_type'] = kwargs['serial_type']
    if 'serial_type' in context and context['serial_type'] == 'tcp':
        if 'telnet_port' in kwargs:
            context['telnet_port'] = kwargs['telnet_port']
        else:
            context['telnet_port'] = 23023  # FIXME: use random unused port
    if 'serial_type' in context:
        if 'console' in kwargs:
            context['console'] = kwargs['console']
        else:
            context['console'] = True

    context['disks'] = {}
    for i, disk in enumerate(diskp):
        for disk_name, args in six.iteritems(disk):
            context['disks'][disk_name] = {}
            fn_ = '{0}.{1}'.format(disk_name, args['format'])
            context['disks'][disk_name]['file_name'] = fn_
            context['disks'][disk_name]['source_file'] = os.path.join(args['pool'],
                                                                      name,
                                                                      fn_)
            if hypervisor in ['qemu', 'kvm']:
                context['disks'][disk_name]['target_dev'] = 'vd{0}'.format(string.ascii_lowercase[i])
                context['disks'][disk_name]['address'] = False
                context['disks'][disk_name]['driver'] = True
            elif hypervisor in ['esxi', 'vmware']:
                context['disks'][disk_name]['target_dev'] = 'sd{0}'.format(string.ascii_lowercase[i])
                context['disks'][disk_name]['address'] = True
                context['disks'][disk_name]['driver'] = False
            context['disks'][disk_name]['disk_bus'] = args['model']
            context['disks'][disk_name]['type'] = args['format']
            context['disks'][disk_name]['index'] = str(i)

    context['nics'] = nicp

    fn_ = 'libvirt_domain.jinja'
    try:
        template = JINJA.get_template(fn_)
    except jinja2.exceptions.TemplateNotFound:
        log.error('Could not load template {0}'.format(fn_))
        return ''

    return template.render(**context)


def _gen_vol_xml(vmname,
                 diskname,
                 size,
                 hypervisor,
                 **kwargs):
    '''
    Generate the XML string to define a libvirt storage volume
    '''
    size = int(size) * 1024  # MB
    disk_info = _get_image_info(hypervisor, vmname, **kwargs)
    context = {
        'name': vmname,
        'filename': '{0}.{1}'.format(diskname, disk_info['disktype']),
        'volname': diskname,
        'disktype': disk_info['disktype'],
        'size': str(size),
        'pool': disk_info['pool'],
    }
    fn_ = 'libvirt_volume.jinja'
    try:
        template = JINJA.get_template(fn_)
    except jinja2.exceptions.TemplateNotFound:
        log.error('Could not load template {0}'.format(fn_))
        return ''
    return template.render(**context)


def _qemu_image_info(path):
    '''
    Detect information for the image at path
    '''
    ret = {}
    out = __salt__['cmd.run']('qemu-img info {0}'.format(path))

    match_map = {'size': r'virtual size: \w+ \((\d+) byte[s]?\)',
                 'format': r'file format: (\w+)'}

    for info, search in six.iteritems(match_map):
        try:
            ret[info] = re.search(search, out).group(1)
        except AttributeError:
            continue
    return ret


# TODO: this function is deprecated, should be replaced with
# _qemu_image_info()
def _image_type(vda):
    '''
    Detect what driver needs to be used for the given image
    '''
    out = __salt__['cmd.run']('qemu-img info {0}'.format(vda))
    if 'file format: qcow2' in out:
        return 'qcow2'
    else:
        return 'raw'


# TODO: this function is deprecated, should be merged and replaced
# with _disk_profile()
def _get_image_info(hypervisor, name, **kwargs):
    '''
    Determine disk image info, such as filename, image format and
    storage pool, based on which hypervisor is used
    '''
    ret = {}
    if hypervisor in ['esxi', 'vmware']:
        ret['disktype'] = 'vmdk'
        ret['filename'] = '{0}{1}'.format(name, '.vmdk')
        ret['pool'] = '[{0}] '.format(kwargs.get('pool', '0'))
    elif hypervisor in ['kvm', 'qemu']:
        ret['disktype'] = 'qcow2'
        ret['filename'] = '{0}{1}'.format(name, '.qcow2')
        ret['pool'] = __salt__['config.option']('virt.images')
    return ret


def _disk_profile(profile, hypervisor, **kwargs):
    '''
    Gather the disk profile from the config or apply the default based
    on the active hypervisor

    This is the ``default`` profile for KVM/QEMU, which can be
    overridden in the configuration:

    .. code-block:: yaml

        virt:
          disk:
            default:
              - system:
                  size: 8192
                  format: qcow2
                  model: virtio

    The ``format`` and ``model`` parameters are optional, and will
    default to whatever is best suitable for the active hypervisor.
    '''
    default = [
          {'system':
             {'size': '8192'}
          }
    ]
    if hypervisor in ['esxi', 'vmware']:
        overlay = {'format': 'vmdk',
                   'model': 'scsi',
                   'pool': '[{0}] '.format(kwargs.get('pool', '0'))
                  }
    elif hypervisor in ['qemu', 'kvm']:
        overlay = {'format': 'qcow2',
                   'model': 'virtio',
                   'pool': __salt__['config.option']('virt.images')
                  }
    else:
        overlay = {}

    disklist = __salt__['config.get']('virt:disk', {}).get(profile, default)
    for key, val in six.iteritems(overlay):
        for i, disks in enumerate(disklist):
            for disk in disks:
                if key not in disks[disk]:
                    disklist[i][disk][key] = val
    return disklist


def _nic_profile(profile_name, hypervisor, **kwargs):

    default = [{'eth0': {}}]
    vmware_overlay = {'type': 'bridge', 'source': 'DEFAULT', 'model': 'e1000'}
    kvm_overlay = {'type': 'bridge', 'source': 'br0', 'model': 'virtio'}
    overlays = {
            'kvm': kvm_overlay,
            'qemu': kvm_overlay,
            'esxi': vmware_overlay,
            'vmware': vmware_overlay,
            }

    # support old location
    config_data = __salt__['config.option']('virt.nic', {}).get(
        profile_name, None
    )

    if config_data is None:
        config_data = __salt__['config.get']('virt:nic', {}).get(
            profile_name, default
        )

    interfaces = []

    def append_dict_profile_to_interface_list(profile_dict):
        for interface_name, attributes in six.iteritems(profile_dict):
            attributes['name'] = interface_name
            interfaces.append(attributes)

    # old style dicts (top-level dicts)
    #
    # virt:
    #    nic:
    #        eth0:
    #            bridge: br0
    #        eth1:
    #            network: test_net
    if isinstance(config_data, dict):
        append_dict_profile_to_interface_list(config_data)

    # new style lists (may contain dicts)
    #
    # virt:
    #   nic:
    #     - eth0:
    #         bridge: br0
    #     - eth1:
    #         network: test_net
    #
    # virt:
    #   nic:
    #     - name: eth0
    #       bridge: br0
    #     - name: eth1
    #       network: test_net
    elif isinstance(config_data, list):
        for interface in config_data:
            if isinstance(interface, dict):
                if len(interface) == 1:
                    append_dict_profile_to_interface_list(interface)
                else:
                    interfaces.append(interface)

    def _normalize_net_types(attributes):
        '''
        Guess which style of definition:

            bridge: br0

             or

            network: net0

             or

            type: network
            source: net0
        '''
        for type_ in ['bridge', 'network']:
            if type_ in attributes:
                attributes['type'] = type_
                # we want to discard the original key
                attributes['source'] = attributes.pop(type_)

        attributes['type'] = attributes.get('type', None)
        attributes['source'] = attributes.get('source', None)

    def _apply_default_overlay(attributes):
        for key, value in six.iteritems(overlays[hypervisor]):
            if key not in attributes or not attributes[key]:
                attributes[key] = value

    def _assign_mac(attributes):
        dmac = kwargs.get('dmac', None)
        if dmac is not None:
            log.debug('DMAC address is {0}'.format(dmac))
            if salt.utils.validate.net.mac(dmac):
                attributes['mac'] = dmac
            else:
                msg = 'Malformed MAC address: {0}'.format(dmac)
                raise CommandExecutionError(msg)
        else:
            attributes['mac'] = salt.utils.gen_mac()

    for interface in interfaces:
        _normalize_net_types(interface)
        _assign_mac(interface)
        if hypervisor in overlays:
            _apply_default_overlay(interface)

    return interfaces


def init(name,
         cpu,
         mem,
         image=None,
         nic='default',
         hypervisor=VIRT_DEFAULT_HYPER,
         start=True,  # pylint: disable=redefined-outer-name
         disk='default',
         saltenv='base',
         seed=True,
         install=True,
         pub_key=None,
         priv_key=None,
         seed_cmd='seed.apply',
         **kwargs):
    '''
    Initialize a new vm

    CLI Example:

    .. code-block:: bash

        salt 'hypervisor' virt.init vm_name 4 512 salt://path/to/image.raw
        salt 'hypervisor' virt.init vm_name 4 512 nic=profile disk=profile
    '''
    hypervisor = __salt__['config.get']('libvirt:hypervisor', hypervisor)
    log.debug('Using hyperisor {0}'.format(hypervisor))

    nicp = _nic_profile(nic, hypervisor, **kwargs)
    log.debug('NIC profile is {0}'.format(nicp))

    diskp = None
    seedable = False
    if image:  # with disk template image
        log.debug('Image {0} will be used'.format(image))
        # if image was used, assume only one disk, i.e. the
        # 'default' disk profile
        # TODO: make it possible to use disk profiles and use the
        # template image as the system disk
        diskp = _disk_profile('default', hypervisor, **kwargs)
        log.debug('Disk profile is {0}'.format(diskp))

        # When using a disk profile extract the sole dict key of the first
        # array element as the filename for disk
        disk_name = next(six.iterkeys(diskp[0]))
        disk_type = diskp[0][disk_name]['format']
        disk_file_name = '{0}.{1}'.format(disk_name, disk_type)

        if hypervisor in ['esxi', 'vmware']:
            # TODO: we should be copying the image file onto the ESX host
            raise SaltInvocationError(
                'virt.init does not support image template template in '
                'conjunction with esxi hypervisor'
            )
        elif hypervisor in ['qemu', 'kvm']:
            img_dir = __salt__['config.option']('virt.images')
            img_dest = os.path.join(
                img_dir,
                name,
                disk_file_name
            )
            img_dir = os.path.dirname(img_dest)
            sfn = __salt__['cp.cache_file'](image, saltenv)
            log.debug('Image directory is {0}'.format(img_dir))

            try:
                os.makedirs(img_dir)
            except OSError:
                pass

            try:
                log.debug('Copying {0} to {1}'.format(sfn, img_dest))
                salt.utils.files.copyfile(sfn, img_dest)
                mask = os.umask(0)
                os.umask(mask)
                # Apply umask and remove exec bit
                mode = (0o0777 ^ mask) & 0o0666
                os.chmod(img_dest, mode)
            except (IOError, OSError) as e:
                raise CommandExecutionError('problem copying image. {0} - {1}'.format(image, e))

            seedable = True
        else:
            log.error('Unsupported hypervisor when handling disk image')
    else:
        # no disk template image specified, create disks based on disk profile
        diskp = _disk_profile(disk, hypervisor, **kwargs)
        log.debug('No image specified, disk profile will be used: {0}'.format(diskp))
        if hypervisor in ['qemu', 'kvm']:
            # TODO: we should be creating disks in the local filesystem with
            # qemu-img
            raise SaltInvocationError(
                'virt.init does not support disk profiles in conjunction with '
                'qemu/kvm at this time, use image template instead'
            )
        else:
            # assume libvirt manages disks for us
            for disk in diskp:
                for disk_name, args in six.iteritems(disk):
                    log.debug('Generating libvirt XML for {0}'.format(disk))
                    xml = _gen_vol_xml(
                        name,
                        disk_name,
                        args['size'],
                        hypervisor,
                    )
                    define_vol_xml_str(xml)

    log.debug('Generating VM XML')
    xml = _gen_xml(name, cpu, mem, diskp, nicp, hypervisor, **kwargs)
    try:
        define_xml_str(xml)
    except libvirtError:
        # This domain already exists
        pass

    if seed and seedable:
        log.debug('Seed command is {0}'.format(seed_cmd))
        __salt__[seed_cmd](
            img_dest,
           id_=name,
           config=kwargs.get('config'),
           install=install,
           pub_key=pub_key,
           priv_key=priv_key,
        )
    if start:
        log.debug('Creating {0}'.format(name))
        create(name)

    return True


def list_vms():
    '''
    Return a list of virtual machine names on the minion

    CLI Example:

    .. code-block:: bash

        salt '*' virt.list_vms
    '''
    vms = []
    vms.extend(list_active_vms())
    vms.extend(list_inactive_vms())
    return vms


def list_active_vms():
    '''
    Return a list of names for active virtual machine on the minion

    CLI Example:

    .. code-block:: bash

        salt '*' virt.list_active_vms
    '''
    conn = __get_conn()
    vms = []
    for id_ in conn.listDomainsID():
        vms.append(conn.lookupByID(id_).name())
    return vms


def list_inactive_vms():
    '''
    Return a list of names for inactive virtual machine on the minion

    CLI Example:

    .. code-block:: bash

        salt '*' virt.list_inactive_vms
    '''
    conn = __get_conn()
    vms = []
    for id_ in conn.listDefinedDomains():
        vms.append(id_)
    return vms


def vm_info(vm_=None):
    '''
    Return detailed information about the vms on this hyper in a
    list of dicts:

    .. code-block:: python

        [
            'your-vm': {
                'cpu': <int>,
                'maxMem': <int>,
                'mem': <int>,
                'state': '<state>',
                'cputime' <int>
                },
            ...
            ]

    If you pass a VM name in as an argument then it will return info
    for just the named VM, otherwise it will return all VMs.

    CLI Example:

    .. code-block:: bash

        salt '*' virt.vm_info
    '''
    def _info(vm_):
        dom = _get_dom(vm_)
        raw = dom.info()
        return {'cpu': raw[3],
                'cputime': int(raw[4]),
                'disks': get_disks(vm_),
                'graphics': get_graphics(vm_),
                'nics': get_nics(vm_),
                'maxMem': int(raw[1]),
                'mem': int(raw[2]),
                'state': VIRT_STATE_NAME_MAP.get(raw[0], 'unknown')}
    info = {}
    if vm_:
        info[vm_] = _info(vm_)
    else:
        for vm_ in list_vms():
            info[vm_] = _info(vm_)
    return info


def vm_state(vm_=None):
    '''
    Return list of all the vms and their state.

    If you pass a VM name in as an argument then it will return info
    for just the named VM, otherwise it will return all VMs.

    CLI Example:

    .. code-block:: bash

        salt '*' virt.vm_state <vm name>
    '''
    def _info(vm_):
        state = ''
        dom = _get_dom(vm_)
        raw = dom.info()
        state = VIRT_STATE_NAME_MAP.get(raw[0], 'unknown')
        return state
    info = {}
    if vm_:
        info[vm_] = _info(vm_)
    else:
        for vm_ in list_vms():
            info[vm_] = _info(vm_)
    return info


def node_info():
    '''
    Return a dict with information about this node

    CLI Example:

    .. code-block:: bash

        salt '*' virt.node_info
    '''
    conn = __get_conn()
    raw = conn.getInfo()
    info = {'cpucores': raw[6],
            'cpumhz': raw[3],
            'cpumodel': str(raw[0]),
            'cpus': raw[2],
            'cputhreads': raw[7],
            'numanodes': raw[4],
            'phymemory': raw[1],
            'sockets': raw[5]}
    return info


def get_nics(vm_):
    '''
    Return info about the network interfaces of a named vm

    CLI Example:

    .. code-block:: bash

        salt '*' virt.get_nics <vm name>
    '''
    nics = {}
    doc = minidom.parse(_StringIO(get_xml(vm_)))
    for node in doc.getElementsByTagName('devices'):
        i_nodes = node.getElementsByTagName('interface')
        for i_node in i_nodes:
            nic = {}
            nic['type'] = i_node.getAttribute('type')
            for v_node in i_node.getElementsByTagName('*'):
                if v_node.tagName == 'mac':
                    nic['mac'] = v_node.getAttribute('address')
                if v_node.tagName == 'model':
                    nic['model'] = v_node.getAttribute('type')
                if v_node.tagName == 'target':
                    nic['target'] = v_node.getAttribute('dev')
                # driver, source, and match can all have optional attributes
                if re.match('(driver|source|address)', v_node.tagName):
                    temp = {}
                    for key, value in v_node.attributes.items():
                        temp[key] = value
                    nic[str(v_node.tagName)] = temp
                # virtualport needs to be handled separately, to pick up the
                # type attribute of the virtualport itself
                if v_node.tagName == 'virtualport':
                    temp = {}
                    temp['type'] = v_node.getAttribute('type')
                    for key, value in v_node.attributes.items():
                        temp[key] = value
                    nic['virtualport'] = temp
            if 'mac' not in nic:
                continue
            nics[nic['mac']] = nic
    return nics


def get_macs(vm_):
    '''
    Return a list off MAC addresses from the named vm

    CLI Example:

    .. code-block:: bash

        salt '*' virt.get_macs <vm name>
    '''
    macs = []
    doc = minidom.parse(_StringIO(get_xml(vm_)))
    for node in doc.getElementsByTagName('devices'):
        i_nodes = node.getElementsByTagName('interface')
        for i_node in i_nodes:
            for v_node in i_node.getElementsByTagName('mac'):
                macs.append(v_node.getAttribute('address'))
    return macs


def get_graphics(vm_):
    '''
    Returns the information on vnc for a given vm

    CLI Example:

    .. code-block:: bash

        salt '*' virt.get_graphics <vm name>
    '''
    out = {'autoport': 'None',
           'keymap': 'None',
           'listen': 'None',
           'port': 'None',
           'type': 'vnc'}
    xml = get_xml(vm_)
    ssock = _StringIO(xml)
    doc = minidom.parse(ssock)
    for node in doc.getElementsByTagName('domain'):
        g_nodes = node.getElementsByTagName('graphics')
        for g_node in g_nodes:
            for key, value in g_node.attributes.items():
                out[key] = value
    return out


def get_disks(vm_):
    '''
    Return the disks of a named vm

    CLI Example:

    .. code-block:: bash

        salt '*' virt.get_disks <vm name>
    '''
    disks = {}
    doc = minidom.parse(_StringIO(get_xml(vm_)))
    for elem in doc.getElementsByTagName('disk'):
        sources = elem.getElementsByTagName('source')
        targets = elem.getElementsByTagName('target')
        if len(sources) > 0:
            source = sources[0]
        else:
            continue
        if len(targets) > 0:
            target = targets[0]
        else:
            continue
        if target.hasAttribute('dev'):
            qemu_target = ''
            if source.hasAttribute('file'):
                qemu_target = source.getAttribute('file')
            elif source.hasAttribute('dev'):
                qemu_target = source.getAttribute('dev')
            elif source.hasAttribute('protocol') and \
                    source.hasAttribute('name'):  # For rbd network
                qemu_target = '{0}:{1}'.format(
                        source.getAttribute('protocol'),
                        source.getAttribute('name'))
            if qemu_target:
                disks[target.getAttribute('dev')] = {
                    'file': qemu_target}
    for dev in disks:
        try:
            hypervisor = __salt__['config.get']('libvirt:hypervisor', 'kvm')
            if hypervisor not in ['qemu', 'kvm']:
                break

            output = []
            stdout = subprocess.Popen(
                        ['qemu-img', 'info', disks[dev]['file']],
                        shell=False,
                        stdout=subprocess.PIPE).communicate()[0]
            qemu_output = salt.utils.to_str(stdout)
            snapshots = False
            columns = None
            lines = qemu_output.strip().split('\n')
            for line in lines:
                if line.startswith('Snapshot list:'):
                    snapshots = True
                    continue

                # If this is a copy-on-write image, then the backing file
                # represents the base image
                #
                # backing file: base.qcow2 (actual path: /var/shared/base.qcow2)
                elif line.startswith('backing file'):
                    matches = re.match(r'.*\(actual path: (.*?)\)', line)
                    if matches:
                        output.append('backing file: {0}'.format(matches.group(1)))
                    continue

                elif snapshots:
                    if line.startswith('ID'):  # Do not parse table headers
                        line = line.replace('VM SIZE', 'VMSIZE')
                        line = line.replace('VM CLOCK', 'TIME VMCLOCK')
                        columns = re.split(r'\s+', line)
                        columns = [c.lower() for c in columns]
                        output.append('snapshots:')
                        continue
                    fields = re.split(r'\s+', line)
                    for i, field in enumerate(fields):
                        sep = ' '
                        if i == 0:
                            sep = '-'
                        output.append(
                            '{0} {1}: "{2}"'.format(
                                sep, columns[i], field
                            )
                        )
                    continue
                output.append(line)
            output = '\n'.join(output)
            disks[dev].update(yaml.safe_load(output))
        except TypeError:
            disks[dev].update(yaml.safe_load('image: Does not exist'))
    return disks


def setmem(vm_, memory, config=False):
    '''
    Changes the amount of memory allocated to VM. The VM must be shutdown
    for this to work.

    memory is to be specified in MB
    If config is True then we ask libvirt to modify the config as well

    CLI Example:

    .. code-block:: bash

        salt '*' virt.setmem myvm 768
    '''
    if vm_state(vm_) != 'shutdown':
        return False

    dom = _get_dom(vm_)

    # libvirt has a funny bitwise system for the flags in that the flag
    # to affect the "current" setting is 0, which means that to set the
    # current setting we have to call it a second time with just 0 set
    flags = libvirt.VIR_DOMAIN_MEM_MAXIMUM
    if config:
        flags = flags | libvirt.VIR_DOMAIN_AFFECT_CONFIG

    ret1 = dom.setMemoryFlags(memory * 1024, flags)
    ret2 = dom.setMemoryFlags(memory * 1024, libvirt.VIR_DOMAIN_AFFECT_CURRENT)

    # return True if both calls succeeded
    return ret1 == ret2 == 0


def setvcpus(vm_, vcpus, config=False):
    '''
    Changes the amount of vcpus allocated to VM. The VM must be shutdown
    for this to work.

    vcpus is an int representing the number to be assigned
    If config is True then we ask libvirt to modify the config as well

    CLI Example:

    .. code-block:: bash

        salt '*' virt.setvcpus myvm 2
    '''
    if vm_state(vm_) != 'shutdown':
        return False

    dom = _get_dom(vm_)

    # see notes in setmem
    flags = libvirt.VIR_DOMAIN_VCPU_MAXIMUM
    if config:
        flags = flags | libvirt.VIR_DOMAIN_AFFECT_CONFIG

    ret1 = dom.setVcpusFlags(vcpus, flags)
    ret2 = dom.setVcpusFlags(vcpus, libvirt.VIR_DOMAIN_AFFECT_CURRENT)

    return ret1 == ret2 == 0


def freemem():
    '''
    Return an int representing the amount of memory (in MB) that has not
    been given to virtual machines on this node

    CLI Example:

    .. code-block:: bash

        salt '*' virt.freemem
    '''
    conn = __get_conn()
    mem = conn.getInfo()[1]
    # Take off just enough to sustain the hypervisor
    mem -= 256
    for vm_ in list_vms():
        dom = _get_dom(vm_)
        if dom.ID() > 0:
            mem -= dom.info()[2] / 1024
    return mem


def freecpu():
    '''
    Return an int representing the number of unallocated cpus on this
    hypervisor

    CLI Example:

    .. code-block:: bash

        salt '*' virt.freecpu
    '''
    conn = __get_conn()
    cpus = conn.getInfo()[2]
    for vm_ in list_vms():
        dom = _get_dom(vm_)
        if dom.ID() > 0:
            cpus -= dom.info()[3]
    return cpus


def full_info():
    '''
    Return the node_info, vm_info and freemem

    CLI Example:

    .. code-block:: bash

        salt '*' virt.full_info
    '''
    return {'freecpu': freecpu(),
            'freemem': freemem(),
            'node_info': node_info(),
            'vm_info': vm_info()}


def get_xml(vm_):
    '''
    Returns the XML for a given vm

    CLI Example:

    .. code-block:: bash

        salt '*' virt.get_xml <vm name>
    '''
    dom = _get_dom(vm_)
    return dom.XMLDesc(0)


def get_profiles(hypervisor=None):
    '''
    Return the virt profiles for hypervisor.

    Currently there are profiles for:

     - nic
     - disk

    CLI Example:

    .. code-block:: bash

        salt '*' virt.get_profiles
        salt '*' virt.get_profiles hypervisor=esxi
    '''
    ret = {}
    if hypervisor:
        hypervisor = hypervisor
    else:
        hypervisor = __salt__['config.get']('libvirt:hypervisor', VIRT_DEFAULT_HYPER)
    virtconf = __salt__['config.get']('virt', {})
    for typ in ['disk', 'nic']:
        _func = getattr(sys.modules[__name__], '_{0}_profile'.format(typ))
        ret[typ] = {'default': _func('default', hypervisor)}
        if typ in virtconf:
            ret.setdefault(typ, {})
            for prf in virtconf[typ]:
                ret[typ][prf] = _func(prf, hypervisor)
    return ret


def shutdown(vm_):
    '''
    Send a soft shutdown signal to the named vm

    CLI Example:

    .. code-block:: bash

        salt '*' virt.shutdown <vm name>
    '''
    dom = _get_dom(vm_)
    return dom.shutdown() == 0


def pause(vm_):
    '''
    Pause the named vm

    CLI Example:

    .. code-block:: bash

        salt '*' virt.pause <vm name>
    '''
    dom = _get_dom(vm_)
    return dom.suspend() == 0


def resume(vm_):
    '''
    Resume the named vm

    CLI Example:

    .. code-block:: bash

        salt '*' virt.resume <vm name>
    '''
    dom = _get_dom(vm_)
    return dom.resume() == 0


def create(vm_):
    '''
    Start a defined domain

    CLI Example:

    .. code-block:: bash

        salt '*' virt.create <vm name>
    '''
    dom = _get_dom(vm_)
    return dom.create() == 0


def start(vm_):
    '''
    Alias for the obscurely named 'create' function

    CLI Example:

    .. code-block:: bash

        salt '*' virt.start <vm name>
    '''
    return create(vm_)


def stop(vm_):
    '''
    Alias for the obscurely named 'destroy' function

    CLI Example:

    .. code-block:: bash

        salt '*' virt.stop <vm name>
    '''
    return destroy(vm_)


def reboot(vm_):
    '''
    Reboot a domain via ACPI request

    CLI Example:

    .. code-block:: bash

        salt '*' virt.reboot <vm name>
    '''
    dom = _get_dom(vm_)

    # reboot has a few modes of operation, passing 0 in means the
    # hypervisor will pick the best method for rebooting
    return dom.reboot(0) == 0


def reset(vm_):
    '''
    Reset a VM by emulating the reset button on a physical machine

    CLI Example:

    .. code-block:: bash

        salt '*' virt.reset <vm name>
    '''
    dom = _get_dom(vm_)

    # reset takes a flag, like reboot, but it is not yet used
    # so we just pass in 0
    # see: http://libvirt.org/html/libvirt-libvirt.html#virDomainReset
    return dom.reset(0) == 0


def ctrl_alt_del(vm_):
    '''
    Sends CTRL+ALT+DEL to a VM

    CLI Example:

    .. code-block:: bash

        salt '*' virt.ctrl_alt_del <vm name>
    '''
    dom = _get_dom(vm_)
    return dom.sendKey(0, 0, [29, 56, 111], 3, 0) == 0


def create_xml_str(xml):
    '''
    Start a domain based on the XML passed to the function

    CLI Example:

    .. code-block:: bash

        salt '*' virt.create_xml_str <XML in string format>
    '''
    conn = __get_conn()
    return conn.createXML(xml, 0) is not None


def create_xml_path(path):
    '''
    Start a domain based on the XML-file path passed to the function

    CLI Example:

    .. code-block:: bash

        salt '*' virt.create_xml_path <path to XML file on the node>
    '''
    try:
        with salt.utils.fopen(path, 'r') as fp_:
            return create_xml_str(fp_.read())
    except (OSError, IOError):
        return False


def define_xml_str(xml):
    '''
    Define a domain based on the XML passed to the function

    CLI Example:

    .. code-block:: bash

        salt '*' virt.define_xml_str <XML in string format>
    '''
    conn = __get_conn()
    return conn.defineXML(xml) is not None


def define_xml_path(path):
    '''
    Define a domain based on the XML-file path passed to the function

    CLI Example:

    .. code-block:: bash

        salt '*' virt.define_xml_path <path to XML file on the node>

    '''
    try:
        with salt.utils.fopen(path, 'r') as fp_:
            return define_xml_str(fp_.read())
    except (OSError, IOError):
        return False


def define_vol_xml_str(xml):
    '''
    Define a volume based on the XML passed to the function

    CLI Example:

    .. code-block:: bash

        salt '*' virt.define_vol_xml_str <XML in string format>
    '''
    poolname = __salt__['config.get']('libvirt:storagepool', 'default')
    conn = __get_conn()
    pool = conn.storagePoolLookupByName(str(poolname))
    return pool.createXML(xml, 0) is not None


def define_vol_xml_path(path):
    '''
    Define a volume based on the XML-file path passed to the function

    CLI Example:

    .. code-block:: bash

        salt '*' virt.define_vol_xml_path <path to XML file on the node>

    '''
    try:
        with salt.utils.fopen(path, 'r') as fp_:
            return define_vol_xml_str(fp_.read())
    except (OSError, IOError):
        return False


def migrate_non_shared(vm_, target, ssh=False):
    '''
    Attempt to execute non-shared storage "all" migration

    CLI Example:

    .. code-block:: bash

        salt '*' virt.migrate_non_shared <vm name> <target hypervisor>
    '''
    cmd = _get_migrate_command() + ' --copy-storage-all ' + vm_\
        + _get_target(target, ssh)

    stdout = subprocess.Popen(cmd,
                shell=True,
                stdout=subprocess.PIPE).communicate()[0]
    return salt.utils.to_str(stdout)


def migrate_non_shared_inc(vm_, target, ssh=False):
    '''
    Attempt to execute non-shared storage "all" migration

    CLI Example:

    .. code-block:: bash

        salt '*' virt.migrate_non_shared_inc <vm name> <target hypervisor>
    '''
    cmd = _get_migrate_command() + ' --copy-storage-inc ' + vm_\
        + _get_target(target, ssh)

    stdout = subprocess.Popen(cmd,
                shell=True,
                stdout=subprocess.PIPE).communicate()[0]
    return salt.utils.to_str(stdout)


def migrate(vm_, target, ssh=False):
    '''
    Shared storage migration

    CLI Example:

    .. code-block:: bash

        salt '*' virt.migrate <vm name> <target hypervisor>
    '''
    cmd = _get_migrate_command() + ' ' + vm_\
        + _get_target(target, ssh)

    stdout = subprocess.Popen(cmd,
                shell=True,
                stdout=subprocess.PIPE).communicate()[0]
    return salt.utils.to_str(stdout)


def seed_non_shared_migrate(disks, force=False):
    '''
    Non shared migration requires that the disks be present on the migration
    destination, pass the disks information via this function, to the
    migration destination before executing the migration.

    CLI Example:

    .. code-block:: bash

        salt '*' virt.seed_non_shared_migrate <disks>
    '''
    for _, data in six.iteritems(disks):
        fn_ = data['file']
        form = data['file format']
        size = data['virtual size'].split()[1][1:]
        if os.path.isfile(fn_) and not force:
            # the target exists, check to see if it is compatible
            pre = yaml.safe_load(subprocess.Popen('qemu-img info arch',
                shell=True,
                stdout=subprocess.PIPE).communicate()[0])
            if pre['file format'] != data['file format']\
                    and pre['virtual size'] != data['virtual size']:
                return False
        if not os.path.isdir(os.path.dirname(fn_)):
            os.makedirs(os.path.dirname(fn_))
        if os.path.isfile(fn_):
            os.remove(fn_)
        cmd = 'qemu-img create -f ' + form + ' ' + fn_ + ' ' + size
        subprocess.call(cmd, shell=True)
        creds = _libvirt_creds()
        cmd = 'chown ' + creds['user'] + ':' + creds['group'] + ' ' + fn_
        subprocess.call(cmd, shell=True)
    return True


def set_autostart(vm_, state='on'):
    '''
    Set the autostart flag on a VM so that the VM will start with the host
    system on reboot.

    CLI Example:

    .. code-block:: bash

        salt "*" virt.set_autostart <vm name> <on | off>
    '''

    dom = _get_dom(vm_)

    if state == 'on':
        return dom.setAutostart(1) == 0

    elif state == 'off':
        return dom.setAutostart(0) == 0

    else:
        # return False if state is set to something other then on or off
        return False


def destroy(vm_):
    '''
    Hard power down the virtual machine, this is equivalent to pulling the
    power

    CLI Example:

    .. code-block:: bash

        salt '*' virt.destroy <vm name>
    '''
    dom = _get_dom(vm_)
    return dom.destroy() == 0


def undefine(vm_):
    '''
    Remove a defined vm, this does not purge the virtual machine image, and
    this only works if the vm is powered down

    CLI Example:

    .. code-block:: bash

        salt '*' virt.undefine <vm name>
    '''
    dom = _get_dom(vm_)
    return dom.undefine() == 0


def purge(vm_, dirs=False):
    '''
    Recursively destroy and delete a virtual machine, pass True for dir's to
    also delete the directories containing the virtual machine disk images -
    USE WITH EXTREME CAUTION!

    CLI Example:

    .. code-block:: bash

        salt '*' virt.purge <vm name>
    '''
    disks = get_disks(vm_)
    try:
        if not destroy(vm_):
            return False
    except libvirt.libvirtError:
        # This is thrown if the machine is already shut down
        pass
    directories = set()
    for disk in disks:
        os.remove(disks[disk]['file'])
        directories.add(os.path.dirname(disks[disk]['file']))
    if dirs:
        for dir_ in directories:
            shutil.rmtree(dir_)
    undefine(vm_)
    return True


def virt_type():
    '''
    Returns the virtual machine type as a string

    CLI Example:

    .. code-block:: bash

        salt '*' virt.virt_type
    '''
    return __grains__['virtual']


def is_kvm_hyper():
    '''
    Returns a bool whether or not this node is a KVM hypervisor

    CLI Example:

    .. code-block:: bash

        salt '*' virt.is_kvm_hyper
    '''
    try:
        with salt.utils.fopen('/proc/modules') as fp_:
            if 'kvm_' not in fp_.read():
                return False
    except IOError:
        # No /proc/modules? Are we on Windows? Or Solaris?
        return False
    return 'libvirtd' in __salt__['cmd.run'](__grains__['ps'])


def is_xen_hyper():
    '''
    Returns a bool whether or not this node is a XEN hypervisor

    CLI Example:

    .. code-block:: bash

        salt '*' virt.is_xen_hyper
    '''
    try:
        if __grains__['virtual_subtype'] != 'Xen Dom0':
            return False
    except KeyError:
        # virtual_subtype isn't set everywhere.
        return False
    try:
        with salt.utils.fopen('/proc/modules') as fp_:
            if 'xen_' not in fp_.read():
                return False
    except (OSError, IOError):
        # No /proc/modules? Are we on Windows? Or Solaris?
        return False
    return 'libvirtd' in __salt__['cmd.run'](__grains__['ps'])


def is_hyper():
    '''
    Returns a bool whether or not this node is a hypervisor of any kind

    CLI Example:

    .. code-block:: bash

        salt '*' virt.is_hyper
    '''
    if HAS_LIBVIRT:
        return is_xen_hyper() or is_kvm_hyper()
    return False


def vm_cputime(vm_=None):
    '''
    Return cputime used by the vms on this hyper in a
    list of dicts:

    .. code-block:: python

        [
            'your-vm': {
                'cputime' <int>
                'cputime_percent' <int>
                },
            ...
            ]

    If you pass a VM name in as an argument then it will return info
    for just the named VM, otherwise it will return all VMs.

    CLI Example:

    .. code-block:: bash

        salt '*' virt.vm_cputime
    '''
    host_cpus = __get_conn().getInfo()[2]

    def _info(vm_):
        dom = _get_dom(vm_)
        raw = dom.info()
        vcpus = int(raw[3])
        cputime = int(raw[4])
        cputime_percent = 0
        if cputime:
            # Divide by vcpus to always return a number between 0 and 100
            cputime_percent = (1.0e-7 * cputime / host_cpus) / vcpus
        return {
                'cputime': int(raw[4]),
                'cputime_percent': int('{0:.0f}'.format(cputime_percent))
               }
    info = {}
    if vm_:
        info[vm_] = _info(vm_)
    else:
        for vm_ in list_vms():
            info[vm_] = _info(vm_)
    return info


def vm_netstats(vm_=None):
    '''
    Return combined network counters used by the vms on this hyper in a
    list of dicts:

    .. code-block:: python

        [
            'your-vm': {
                'rx_bytes'   : 0,
                'rx_packets' : 0,
                'rx_errs'    : 0,
                'rx_drop'    : 0,
                'tx_bytes'   : 0,
                'tx_packets' : 0,
                'tx_errs'    : 0,
                'tx_drop'    : 0
                },
            ...
            ]

    If you pass a VM name in as an argument then it will return info
    for just the named VM, otherwise it will return all VMs.

    CLI Example:

    .. code-block:: bash

        salt '*' virt.vm_netstats
    '''
    def _info(vm_):
        dom = _get_dom(vm_)
        nics = get_nics(vm_)
        ret = {
                'rx_bytes': 0,
                'rx_packets': 0,
                'rx_errs': 0,
                'rx_drop': 0,
                'tx_bytes': 0,
                'tx_packets': 0,
                'tx_errs': 0,
                'tx_drop': 0
               }
        for attrs in six.itervalues(nics):
            if 'target' in attrs:
                dev = attrs['target']
                stats = dom.interfaceStats(dev)
                ret['rx_bytes'] += stats[0]
                ret['rx_packets'] += stats[1]
                ret['rx_errs'] += stats[2]
                ret['rx_drop'] += stats[3]
                ret['tx_bytes'] += stats[4]
                ret['tx_packets'] += stats[5]
                ret['tx_errs'] += stats[6]
                ret['tx_drop'] += stats[7]

        return ret
    info = {}
    if vm_:
        info[vm_] = _info(vm_)
    else:
        for vm_ in list_vms():
            info[vm_] = _info(vm_)
    return info


def vm_diskstats(vm_=None):
    '''
    Return disk usage counters used by the vms on this hyper in a
    list of dicts:

    .. code-block:: python

        [
            'your-vm': {
                'rd_req'   : 0,
                'rd_bytes' : 0,
                'wr_req'   : 0,
                'wr_bytes' : 0,
                'errs'     : 0
                },
            ...
            ]

    If you pass a VM name in as an argument then it will return info
    for just the named VM, otherwise it will return all VMs.

    CLI Example:

    .. code-block:: bash

        salt '*' virt.vm_blockstats
    '''
    def get_disk_devs(vm_):
        doc = minidom.parse(_StringIO(get_xml(vm_)))
        disks = []
        for elem in doc.getElementsByTagName('disk'):
            targets = elem.getElementsByTagName('target')
            target = targets[0]
            disks.append(target.getAttribute('dev'))
        return disks

    def _info(vm_):
        dom = _get_dom(vm_)
        # Do not use get_disks, since it uses qemu-img and is very slow
        # and unsuitable for any sort of real time statistics
        disks = get_disk_devs(vm_)
        ret = {'rd_req': 0,
               'rd_bytes': 0,
               'wr_req': 0,
               'wr_bytes': 0,
               'errs': 0
               }
        for disk in disks:
            stats = dom.blockStats(disk)
            ret['rd_req'] += stats[0]
            ret['rd_bytes'] += stats[1]
            ret['wr_req'] += stats[2]
            ret['wr_bytes'] += stats[3]
            ret['errs'] += stats[4]

        return ret
    info = {}
    if vm_:
        info[vm_] = _info(vm_)
    else:
        # Can not run function blockStats on inactive VMs
        for vm_ in list_active_vms():
            info[vm_] = _info(vm_)
    return info
