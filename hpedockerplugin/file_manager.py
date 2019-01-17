import json
import string
import os
import six
import time
import uuid
from sh import chmod
from Crypto.Cipher import AES
import base64

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import importutils
from oslo_utils import netutils
from oslo_utils import units
from twisted.python.filepath import FilePath

import hpedockerplugin.exception as exception
import hpedockerplugin.fileutil as fileutil
import math
import re
import hpedockerplugin.hpe.array_connection_params as acp
import datetime
from hpedockerplugin.hpe import utils
from hpedockerplugin.i18n import _, _LE, _LI, _LW
import hpedockerplugin.synchronization as synchronization
from hpedockerplugin.hpe import hpe_3par_mediator

LOG = logging.getLogger(__name__)
PRIMARY = 1
PRIMARY_REV = 1
SECONDARY = 2

CONF = cfg.CONF

import pdb


class FileManager(object):
    def __init__(self, host_config, hpepluginconfig, etcd_util,
                 backend_name='DEFAULT'):
        self._host_config = host_config
        self._hpepluginconfig = hpepluginconfig
        self._my_ip = netutils.get_my_ipv4()

        self._etcd = etcd_util

        self._initialize_configuration()

        # self._decrypt_password(self.src_bkend_config,
        #                        self.tgt_bkend_config, backend_name)

        # TODO: When multiple backends come into picture, consider
        # lazy initialization of individual driver
        try:
            LOG.info("Initializing 3PAR driver...")
            self._primary_driver = self._initialize_driver(
                host_config, self.src_bkend_config, self.tgt_bkend_config)

            self._hpeplugin_driver = self._primary_driver
            LOG.info("Initialized 3PAR driver!")
        except Exception as ex:
            msg = "Failed to initialize 3PAR driver for array: %s!" \
                  "Exception: %s"\
                  % (self.src_bkend_config.hpe3par_api_url,
                     six.text_type(ex))
            LOG.info(msg)
            raise exception.HPEPluginStartPluginException(
                reason=msg)

        # If replication enabled, then initialize secondary driver
        if self.tgt_bkend_config:
            LOG.info("Replication enabled!")
            try:
                LOG.info("Initializing 3PAR driver for remote array...")
                self._remote_driver = self._initialize_driver(
                    host_config, self.tgt_bkend_config,
                    self.src_bkend_config)
            except Exception as ex:
                msg = "Failed to initialize 3PAR driver for remote array %s!" \
                      "Exception: %s"\
                      % (self.tgt_bkend_config.hpe3par_api_url,
                         six.text_type(ex))
                LOG.info(msg)
                raise exception.HPEPluginStartPluginException(reason=msg)

        self._node_id = self._get_node_id()

    @staticmethod
    def _get_node_id():
        # Save node-id if it doesn't exist
        node_id_file_path = '/etc/hpedockerplugin/.node_id'
        if not os.path.isfile(node_id_file_path):
            node_id = str(uuid.uuid4())
            with open(node_id_file_path, 'w') as node_id_file:
                node_id_file.write(node_id)
        else:
            with open(node_id_file_path, 'r') as node_id_file:
                node_id = node_id_file.readline()
        return node_id

    def _initialize_configuration(self):
        self.src_bkend_config = self._get_src_bkend_config()

        self.tgt_bkend_config = None
        # if self._hpepluginconfig.replication_device:
        #     self.tgt_bkend_config = acp.ArrayConnectionParams(
        #         self._hpepluginconfig.replication_device)
        #     if self.tgt_bkend_config:
        #
        #         # Copy all the source configuration to target
        #         hpeconf = self._hpepluginconfig
        #         for key in hpeconf.keys():
        #             if not self.tgt_bkend_config.is_param_present(key):
        #                 value = getattr(hpeconf, key)
        #                 self.tgt_bkend_config.__setattr__(key, value)

    def _get_src_bkend_config(self):
        LOG.info("Getting source backend configuration...")
        hpeconf = self._hpepluginconfig
        config = acp.ArrayConnectionParams()
        for key in hpeconf.keys():
            value = getattr(hpeconf, key)
            config.__setattr__(key, value)

        LOG.info("Got source backend configuration!")
        return config

    def _initialize_driver(self, host_config, src_config, tgt_config):

        mediator = self.create_mediator(host_config, src_config)
        try:
            mediator.do_setup(timeout=30)
            # self.check_for_setup_error()
            return mediator
        except Exception as ex:
            msg = (_('hpeplugin_driver do_setup failed, error is: %s'),
                   six.text_type(ex))
            LOG.error(msg)
            raise exception.HPEPluginNotInitializedException(reason=msg)

    def create_mediator(self, host_config, config):
        """Any initialization the share driver does while starting."""

        # LOG.info("Starting share driver %(driver_name)s (%(version)s)",
        #          {'driver_name': self.__class__.__name__,
        #           'version': self.VERSION})
        return hpe_3par_mediator.HPE3ParMediator(host_config, config)

    def create_share_old(self, share_details):
        return self._synchronized_create_share(share_details['shareName'],
                                               share_details)

    def create_share(self, share_details):
        share_name = share_details['name']
        self._synchronized_create_share(share_name, share_details)

    # @synchronization.synchronized_volume('{share_name}')
    def _synchronized_create_share(self, share_name, share_details):
        db_share = self._etcd.get_vol_byname(share_name)
        if db_share is not None:
            return json.dumps({u"Err": ''})

        undo_steps = []
        try:
            self._hpeplugin_driver.create_share(share_details)
            self._etcd.save_vol(share_details)
        except Exception as ex:
            msg = (_('Create share failed with error: %s'), six.text_type(ex))
            LOG.exception(msg)
            self._rollback(undo_steps)
            return json.dumps({u"Err": six.text_type(ex)})
        else:
            LOG.info('Share: %(name)s was successfully saved to etcd',
                     {'name': share_name})
            return json.dumps({u"Err": ''})

    @synchronization.synchronized_volume('{share_name}')
    def remove_share(self, share_name):
        share = self._etcd.get_vol_byname(share_name)
        if share is None:
            # Just log an error, but don't fail the docker rm command
            msg = 'Share name to remove not found: %s' % share_name
            LOG.error(msg)
            return json.dumps({u"Err": msg})

        protocol = share['protocol']
        vfs = share['vfs']
        fpg = share['fpg']
        fstore = share['fstore']
        share_ip = share['vfsIP']

        try:
            self._hpeplugin_driver.delete_share(share)
        except Exception as e:
            msg = (_('Failed to remove share %(share_name)s: %(e)s') %
                   {'share_name': share_name, 'e': six.text_type(e)})
            LOG.exception(msg)
            raise exception.ShareBackendException(msg=msg)

    @synchronization.synchronized_volume('{share_name}')
    def remove_snapshot(self, share_name, snapname):
        pass

    def get_share_details(self, share_name):
        db_share = self._etcd.get_vol_byname(share_name)
        LOG.info("Share details: %s", db_share)
        if db_share is None:
            msg = (_LE('Share Get: Share name not found %s'), share_name)
            LOG.warning(msg)
            response = json.dumps({u"Err": ""})
            return response

        err = ''
        mountdir = ''
        devicename = ''

        path_info = self._etcd.get_vol_path_info(share_name)
        if path_info is not None:
            mountdir = path_info['mount_dir']
            devicename = path_info['path']

        # use volinfo as volname could be partial match
        share = {'Name': share_name,
                 'Mountpoint': mountdir,
                 'Devicename': devicename,
                 'Status': db_share}
        response = json.dumps({u"Err": err, u"Volume": share})
        LOG.debug("Get share: \n%s" % str(response))
        return response

    def list_shares(self):
        db_shares = self._etcd.get_all_vols()

        if not db_shares:
            response = json.dumps({u"Err": ''})
            return response

        share_list = []
        for db_share in db_shares:
            path_info = self._etcd.get_path_info_from_vol(db_share)
            if path_info is not None and 'mount_dir' in path_info:
                mountdir = path_info['mount_dir']
                devicename = path_info['path']
            else:
                mountdir = ''
                devicename = ''
            share = {'Name': db_share['shareName'],
                     'Devicename': devicename,
                     'size': db_share['hardQuota'],
                     'Mountpoint': mountdir,
                     'Status': db_share}
            share_list.append(share)

        response = json.dumps({u"Err": '', u"Volumes": share_list})
        return response

    def get_path(self, volname):
        pass

    @staticmethod
    def _is_share_not_mounted(share):
        return 'node_mount_info' not in share

    def _is_share_mounted_on_this_node(self, node_mount_info):
        return self._node_id in node_mount_info

    def _update_mount_id_list(self, share, mount_id):
        node_mount_info = share['node_mount_info']

        # Check if mount_id is unique
        if mount_id in node_mount_info[self._node_id]:
            LOG.info("Received duplicate mount-id: %s. Ignoring"
                     % mount_id)
            return

        LOG.info("Adding new mount-id %s to node_mount_info..."
                 % mount_id)
        node_mount_info[self._node_id].append(mount_id)
        LOG.info("Updating etcd with modified node_mount_info: %s..."
                 % node_mount_info)
        self._etcd.update_vol(share['id'],
                              'node_mount_info',
                              node_mount_info)
        LOG.info("Updated etcd with modified node_mount_info: %s!"
                 % node_mount_info)

    def _get_success_response(self, vol):
        path_info = json.loads(vol['path_info'])
        path = FilePath(path_info['device_info']['path']).realpath()
        response = json.dumps({"Err": '', "Name": vol['display_name'],
                               "Mountpoint": path_info['mount_dir'],
                               "Devicename": path.path})
        return response

    @synchronization.synchronized_volume('{share_name}')
    def mount_share(self, share_name, share_mount, mount_id):
        LOG.info("Inside mount share... getting share by name: %s" % share_name)
        share = self._etcd.get_vol_byname(share_name)
        if share is None:
            msg = (_LE('Volume mount name not found %s'), share_name)
            LOG.error(msg)
            raise exception.HPEPluginMountException(reason=msg)

        def _get_mount_path():
            path = '/opt/hpe/data/hpedocker-%s-%s' % (share_name, mount_id)
            # if share['shareDir']:
            #     path = '/'.join([prefix, share['shareDir'], mount_id])
            # else:
            #     path = '/'.join([prefix, share['shareName'], mount_id])
            return path

        # mount_dir = _get_mount_path()
        mount_dir = '/opt/hpe/data/hpedocker-%s-%s' % (share_name, mount_id)
        # TODO: Check instead if mount entry is there and based on that
        # decide
        # if os.path.exists(mount_dir):
        #     msg = "Mount path %s already in use" % mount_dir
        #     raise exception.HPEPluginMountException(reason=msg)

        LOG.info('Creating Directory %(mount_dir)s...',
                 {'mount_dir': mount_dir})
        fileutil.make_dir(mount_dir)
        LOG.info('Directory: %(mount_dir)s successfully created!',
                 {'mount_dir': mount_dir})

        # mount the directory
        # TODO: Imran: next(iter()) needs to be just share['fpg']
        share_path = "%s:/%s/%s/%s/%s" %(share['vfsIP'],
                                         next(iter(share['fpg'][0])),
                                         share['vfs'],
                                         share['fstore'],
                                         share['shareDir'])
        LOG.info("Mounting share path %s to %s" % (share_path, mount_dir))
        fileutil.mount_dir(share_path, mount_dir)
        LOG.debug('Device: %(path)s successfully mounted on %(mount)s',
                  {'path': share_path, 'mount': mount_dir})

        # if True:
        #     msg = (_LE('FAILING MOUNT OPERATION %s'), share_name)
        #     LOG.error(msg)
        #     raise exception.HPEPluginMountException(reason=msg)

        # if 'fsOwner' in share and share['fsOwner']:
        #     fs_owner = share['fsOwner'].split(":")
        #     uid = int(fs_owner[0])
        #     gid = int(fs_owner[1])
        #     os.chown(mount_dir, uid, gid)
        #
        # if 'fsMode' in share and share['fsMode']:
        #     mode = str(share['fsMode'])
        #     chmod(mode, mount_dir)

        if 'mount_path_dict' not in share:
            mount_path_dict = {mount_id: mount_dir}
            share['mount_path_dict'] = mount_path_dict
        else:
            mount_path_dict = share['mount_path_dict']
            mount_path_dict[mount_id] = mount_dir

        self._etcd.update_vol(share['id'], 'mount_path_dict', mount_path_dict)

        response = json.dumps({u"Err": '', u"Name": share_name,
                               u"Mountpoint": mount_dir,
                               u"Devicename": share_path})
        return response

    @synchronization.synchronized_volume('{share_name}')
    def unmount_share(self, share_name, share_mount, mount_id):
        share = self._etcd.get_vol_byname(share_name)
        if share is None:
            msg = (_LE('Share unmount name not found %s'), share_name)
            LOG.error(msg)
            raise exception.HPEPluginUMountException(reason=msg)

        share_id = share['id']

        # Start of volume fencing
        LOG.info('Unmounting share: %s' % share)
        mount_path_dict = share.get('mount_path_dict')
        if mount_path_dict:
            mount_path = mount_path_dict[mount_id]
            LOG.info('Unmounting dir: %s' % mount_path)
            fileutil.umount_dir(mount_path)
            LOG.info('Removing dir: %s' % mount_path)
            fileutil.remove_dir(mount_path)
            LOG.info('Removing path info from meta-data: %s' % mount_path)
            del mount_path_dict[mount_id]
            if not mount_path_dict:
                del share['mount_path_dict']
            LOG.info('Updating path info in meta-data: %s' % mount_path)
            self._etcd.save_vol(share)

        response = json.dumps({u"Err": ''})
        LOG.info('Unmount DONE for share: %s, %s' % (share_name, mount_id))
        return response

    def import_share(self, volname, existing_ref, backend='DEFAULT',
                     manage_opts=None):
        pass

    @staticmethod
    def _rollback(rollback_list):
        for undo_action in reversed(rollback_list):
            LOG.info(undo_action['msg'])
            try:
                undo_action['undo_func'](**undo_action['params'])
            except Exception as ex:
                # TODO: Implement retry logic
                LOG.exception('Ignoring exception: %s' % ex)
                pass

    def _decrypt(self, encrypted, passphrase):
        aes = AES.new(passphrase, AES.MODE_CFB, '1234567812345678')
        decrypt_pass = aes.decrypt(base64.b64decode(encrypted))
        return decrypt_pass.decode('utf-8')

    def _decrypt_password(self, src_bknd, trgt_bknd, backend_name):
        try:
            passphrase = self._etcd.get_backend_key(backend_name)
        except Exception as ex:
            LOG.info('Exception occurred %s ' % ex)
            LOG.info("Using PLAIN TEXT for backend '%s'" % backend_name)
        else:
            passphrase = self.key_check(passphrase)
            src_bknd.hpe3par_password = \
                self._decrypt(src_bknd.hpe3par_password, passphrase)
            src_bknd.san_password =  \
                self._decrypt(src_bknd.san_password, passphrase)
            if trgt_bknd:
                trgt_bknd.hpe3par_password = \
                    self._decrypt(trgt_bknd.hpe3par_password, passphrase)
                trgt_bknd.san_password = \
                    self._decrypt(trgt_bknd.san_password, passphrase)

    def key_check(self, key):
        KEY_LEN = len(key)
        padding_string = string.ascii_letters

        if KEY_LEN < 16:
            KEY = key + padding_string[:16 - KEY_LEN]

        elif KEY_LEN > 16 and KEY_LEN < 24:
            KEY = key + padding_string[:24 - KEY_LEN]

        elif KEY_LEN > 24 and KEY_LEN < 32:
            KEY = key + padding_string[:32 - KEY_LEN]

        elif KEY_LEN > 32:
            KEY = key[:32]

        return KEY
