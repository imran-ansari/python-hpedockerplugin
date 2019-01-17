import abc
import json
import re
import six
from collections import OrderedDict

from oslo_log import log as logging

import hpedockerplugin.exception as exception
from hpedockerplugin.hpe import volume
from hpedockerplugin.hpe import share

LOG = logging.getLogger(__name__)


class RequestParserFactory(object):
    def __init__(self, all_configs):
        self._all_configs = all_configs

        if 'block' in all_configs:
            block_configs = all_configs['block']
            backend_configs = block_configs[1]
            self._vol_req_parser = VolumeRequestParser(backend_configs)
        else:
            self._vol_req_parser = NullRequestParser(
                "ERROR: Volume driver not enabled. Please provide hpe.conf "
                "file to enable it")

        if 'file' in all_configs:
            file_configs = all_configs['file']
            f_backend_configs = file_configs[1]
            self._file_req_parser = FileRequestParser(f_backend_configs)
        else:
            self._file_req_parser = NullRequestParser(
                "ERROR: File driver not enabled. Please provide hpe_file.conf "
                "file to enable it")

    def get_request_parser(self, contents):
        if 'Opts' in contents and contents['Opts']:
            if 'persona' in contents['Opts']:
                persona = contents['Opts']['persona']
                if persona == 'file':
                    return self._file_req_parser
                else:
                    msg = "Invalid value '%s' specified for 'persona'. " \
                          "Valid values are ['volume', 'file']" % persona
                    LOG.error(msg)
                    raise exception.InvalidInput(reason=msg)
        return self._vol_req_parser


class NullRequestParser(object):
    def __init__(self, msg):
        self._msg = msg

    def parse_request(self, contents):
        raise exception.InvalidInput(self._msg)


class RequestParser(object):
    def __init__(self, backend_configs):
        self._backend_configs = backend_configs

    def parse_request(self, contents):
        self._validate_name(contents['Name'])

        parser_map = self._get_parser_map()

        if 'Opts' in contents and contents['Opts']:
            self._validate_mutually_exclusive_ops(contents)

            for op_name, parser_func in parser_map.items():
                op_name = op_name.split(',')
                found = not (set(op_name) - set(contents['Opts'].keys()))
                if found:
                    return parser_func(contents)
        return self._default_parse_request(contents)

    @staticmethod
    def _validate_name(vol_name):
        is_valid_name = re.match("^[A-Za-z0-9]+[A-Za-z0-9_-]+$", vol_name)
        if not is_valid_name:
            msg = 'Invalid volume name: %s is passed.' % vol_name
            raise exception.InvalidInput(reason=msg)

    @staticmethod
    def _get_int_option(options, option_name, default_val):
        opt = options.get(option_name)
        if opt and opt != '':
            try:
                opt = int(opt)
            except ValueError as ex:
                msg = "ERROR: Invalid value '%s' specified for '%s' option. " \
                      "Please specify an integer value." % (opt, option_name)
                LOG.error(msg)
                raise exception.InvalidInput(msg)
        else:
            opt = default_val
        return opt

    # This method does the following:
    # 1. Option specified
    #  - Some value:
    #    -- return if valid value else exception
    #  - Blank value:
    #    -- Return default if provided
    #       ELSE
    #    -- Throw exception if value_unset_exception is set
    # 2. Option NOT specified
    #   - Return default value
    @staticmethod
    def _get_str_option(options, option_name, default_val, valid_values=None,
                        value_unset_exception=False):
        opt = options.get(option_name)
        if opt:
            if opt != '':
                opt = str(opt)
                if valid_values and opt.lower() not in valid_values:
                    msg = "ERROR: Invalid value '%s' specified for '%s' option. " \
                          "Valid values are: %s" % (opt, option_name, valid_values)
                    LOG.error(msg)
                    raise exception.InvalidInput(msg)

                return opt

            if default_val:
                return default_val

            if value_unset_exception:
                return json.dumps({
                    'Err': "Value not set for option: %s" % opt
                })
        return default_val

    # To be implemented by derived class
    @abc.abstractmethod
    def _get_parser_map(self):
        pass

    def _default_parse_request(self, contents):
        pass

    @staticmethod
    def _validate_mutually_exclusive_ops(contents):
        mutually_exclusive_ops = ['virtualCopyOf', 'cloneOf', 'importVol',
                                  'replicationGroup']
        if 'Opts' in contents and contents['Opts']:
            received_opts = contents.get('Opts').keys()
            diff = set(mutually_exclusive_ops) - set(received_opts)
            if len(diff) < len(mutually_exclusive_ops) - 1:
                mutually_exclusive_ops.sort()
                msg = "Operations %s are mutually exclusive and cannot be " \
                      "specified together. Please check help for usage." % \
                      mutually_exclusive_ops
                raise exception.InvalidInput(reason=msg)

    @staticmethod
    def _validate_opts(operation, contents, valid_opts, mandatory_opts=None):
        if 'Opts' in contents and contents['Opts']:
            received_opts = contents.get('Opts').keys()

            if mandatory_opts:
                diff = set(mandatory_opts) - set(received_opts)
                if diff:
                    # Print options in sorted manner
                    mandatory_opts.sort()
                    msg = "One or more mandatory options %s are missing " \
                          "for operation %s" % (mandatory_opts, operation)
                    raise exception.InvalidInput(reason=msg)

            diff = set(received_opts) - set(valid_opts)
            if diff:
                diff = list(diff)
                diff.sort()
                msg = "Invalid option(s) %s specified for operation %s. " \
                      "Please check help for usage." % \
                      (diff, operation)
                raise exception.InvalidInput(reason=msg)


class FileRequestParser(RequestParser):
    def __init__(self, backend_configs):
        super(FileRequestParser, self).__init__(backend_configs)

    def _get_parser_map(self):
        parser_map = OrderedDict()
        parser_map['fpg,vfs,fileStore'] = \
            self._parse_share_opts
        parser_map['virtualCopyOf,shareName'] = \
            self._parse_snapshot_opts
        parser_map['updateShare'] = \
            self._parse_update_opts
        parser_map['help'] = self._parse_help_opt
        return parser_map

    def _create_share_metadata(self, name, options):
        backend = self._get_str_option(options, 'backend', 'DEFAULT')
        fpg = self._get_str_option(options, 'fpg', None,
                                   value_unset_exception=True)
        vfs = self._get_str_option(options, 'vfs', '%s_vfs' % name)
        file_store = self._get_str_option(options, 'fileStore',
                                          '%s_fstore' % name)
        # TODO: Check if create share requires None or something else to
        # create share at File Store level
        share_dir = self._get_str_option(options, 'shareDir', None)

        config = self._backend_configs[backend]
        cfg_fpgs = config['hpe3par_fpg']
        vfs_ip = None
        for cfg_fpg in cfg_fpgs:
            try:
                vfs_ip = cfg_fpg[fpg]
            except KeyError as ex:
                raise exception.InvalidInput("ERROR: Specified FPG doesn't "
                                             "exist in the configuration file."
                                             "Available FPGs: %s" % cfg_fpgs)
        share_details = share.create_metadata(backend, fpg, vfs, file_store,
                                              name, vfs_ip)
        return share_details

    def _parse_share_opts(self, contents):
        valid_opts = ['fpg', 'vfs', 'fileStore', 'shareDir', 'shareName',
                      'backend', 'persona', 'readonly']
        self._validate_opts("create share", contents, valid_opts)
        share_details = self._create_share_metadata(contents['Name'],
                                                    contents['Opts'])
        return {'orchestrator': 'file',
                'operation': 'create_share',
                'kwargs': share_details}

    def _parse_snapshot_opts(self, contents):
        pass

    def _parse_update_opts(self, contents):
        pass

    def _parse_help_opt(self, contents):
        pass


class VolumeRequestParser(RequestParser):
    def __init__(self, backend_configs):
        super(VolumeRequestParser, self).__init__(backend_configs)

    def _get_parser_map(self):
        parser_map = OrderedDict()
        parser_map['virtualCopyOf,scheduleName'] = \
            self._parse_snapshot_schedule_opts,
        parser_map['virtualCopyOf,scheduleFrequency'] = \
            self._parse_snapshot_schedule_opts
        parser_map['virtualCopyOf,snaphotPrefix'] = \
            self._parse_snapshot_schedule_opts
        parser_map['virtualCopyOf'] = \
            self._parse_snapshot_opts
        parser_map['cloneOf'] = \
            self._parse_clone_opts
        parser_map['importVol'] = \
            self._parse_import_vol_opts
        parser_map['replicationGroup'] = \
            self._parse_rcg_opts
        parser_map['help'] = self._parse_help_opt
        return parser_map

    def _default_parse_request(self, contents):
        return  self._parse_create_volume_opts(contents)

    @staticmethod
    def _validate_mutually_exclusive_ops(contents):
        mutually_exclusive_ops = ['virtualCopyOf', 'cloneOf', 'importVol',
                                  'replicationGroup']
        if 'Opts' in contents and contents['Opts']:
            received_opts = contents.get('Opts').keys()
            diff = set(mutually_exclusive_ops) - set(received_opts)
            if len(diff) < len(mutually_exclusive_ops) - 1:
                mutually_exclusive_ops.sort()
                msg = "Operations %s are mutually exclusive and cannot be " \
                      "specified together. Please check help for usage." % \
                      mutually_exclusive_ops
                raise exception.InvalidInput(reason=msg)

    @staticmethod
    def _validate_opts(operation, contents, valid_opts, mandatory_opts=None):
        if 'Opts' in contents and contents['Opts']:
            received_opts = contents.get('Opts').keys()

            if mandatory_opts:
                diff = set(mandatory_opts) - set(received_opts)
                if diff:
                    # Print options in sorted manner
                    mandatory_opts.sort()
                    msg = "One or more mandatory options %s are missing " \
                          "for operation %s" % (mandatory_opts, operation)
                    raise exception.InvalidInput(reason=msg)

            diff = set(received_opts) - set(valid_opts)
            if diff:
                diff = list(diff)
                diff.sort()
                msg = "Invalid option(s) %s specified for operation %s. " \
                      "Please check help for usage." % \
                      (diff, operation)
                raise exception.InvalidInput(reason=msg)

    def _parse_create_volume_opts(self, contents):
        valid_opts = ['compression', 'size', 'provisioning',
                      'flash-cache', 'qos-name', 'fsOwner',
                      'fsMode', 'mountConflictDelay', 'cpg',
                      'snapcpg', 'backend']
        self._validate_opts("create volume", contents, valid_opts)
        return {'operation': 'create_volume',
                '_vol_orchestrator': 'volume'}

    def _parse_clone_opts(self, contents):
        valid_opts = ['cloneOf', 'size', 'cpg', 'snapcpg',
                      'mountConflictDelay']
        self._validate_opts("clone volume", contents, valid_opts)
        return {'operation': 'clone_volume',
                '_vol_orchestrator': 'volume'}

    def _parse_snapshot_opts(self, contents):
        valid_opts = ['virtualCopyOf', 'retentionHours', 'expirationHours',
                      'mountConflictDelay', 'size']
        self._validate_opts("create snapshot", contents, valid_opts)
        return {'operation': 'create_snapshot',
                '_vol_orchestrator': 'volume'}

    def _parse_snapshot_schedule_opts(self, contents):
        valid_opts = ['virtualCopyOf', 'scheduleFrequency', 'scheduleName',
                      'snapshotPrefix', 'expHrs', 'retHrs',
                      'mountConflictDelay', 'size']
        mandatory_opts = ['scheduleName', 'snapshotPrefix',
                          'scheduleFrequency']
        self._validate_opts("create snapshot schedule", contents,
                            valid_opts, mandatory_opts)
        return {'operation': 'create_snapshot_schedule',
                '_vol_orchestrator': 'volume'}

    def _parse_import_vol_opts(self, contents):
        valid_opts = ['importVol', 'backend', 'mountConflictDelay']
        self._validate_opts("import volume", contents, valid_opts)

        # Replication enabled backend cannot be used for volume import
        backend = contents['Opts'].get('backend', 'DEFAULT')
        if backend == '':
            backend = 'DEFAULT'

        try:
            config = self._backend_configs[backend]
        except KeyError:
            backend_names = list(self._backend_configs.keys())
            backend_names.sort()
            msg = "ERROR: Backend '%s' doesn't exist. Available " \
                  "backends are %s. Please use " \
                  "a valid backend name and retry." % \
                  (backend, backend_names)
            raise exception.InvalidInput(reason=msg)

        if config.replication_device:
            msg = "ERROR: Import volume not allowed with replication " \
                  "enabled backend '%s'" % backend
            raise exception.InvalidInput(reason=msg)

        volname = contents['Name']
        existing_ref = str(contents['Opts']['importVol'])
        manage_opts = contents['Opts']
        return {'_vol_orchestrator': 'volume',
                'operation': 'import_volume',
                'args': (volname,
                         existing_ref,
                         backend,
                         manage_opts)}

    def _parse_rcg_opts(self, contents):
        valid_opts = ['replicationGroup', 'size', 'provisioning',
                      'backend', 'mountConflictDelay', 'compression']
        self._validate_opts('create replicated volume', contents, valid_opts)

        # It is possible that the user configured replication in hpe.conf
        # but didn't specify any options. In that case too, this operation
        # must fail asking for "replicationGroup" parameter
        # Hence this validation must be done whether "Opts" is there or not
        options = contents['Opts']
        backend = self._get_str_option(options, 'backend', 'DEFAULT')
        create_vol_args = self._get_create_volume_args(options)
        rcg_name = create_vol_args['replicationGroup']
        try:
            self._validate_rcg_params(rcg_name, backend)
        except exception.InvalidInput as ex:
            return json.dumps({u"Err": ex.msg})

        return {'operation': 'create_volume',
                '_vol_orchestrator': 'volume',
                'args': create_vol_args}

    def _get_fs_owner(self, options):
        fs_owner = self._get_str_option(options, 'fsOwner', None)
        if fs_owner:
            try:
                mode = fs_owner.split(':')
            except ValueError as ex:
                return json.dumps({'Err': "Invalid value '%s' specified "
                                          "for fsOwner. Please "
                                          "specify a correct value." %
                                          fs_owner})
            except IndexError as ex:
                return json.dumps({'Err': "Invalid value '%s' specified "
                                          "for fsOwner. Please "
                                          "specify both uid and gid." %
                                          fs_owner})
            return fs_owner
        return None

    def _get_fs_mode(self, options):
        fs_mode_str = self._get_str_option(options, 'fsMode', None)
        if fs_mode_str:
            try:
                int(fs_mode_str)
            except ValueError as ex:
                return json.dumps({'Err': "Invalid value '%s' specified "
                                          "for fsMode. Please "
                                          "specify an integer value." %
                                          fs_mode_str})
            if fs_mode_str[0] != '0':
                return json.dumps({'Err': "Invalid value '%s' specified "
                                          "for fsMode. Please "
                                          "specify an octal value." %
                                          fs_mode_str})
            for mode in fs_mode_str:
                if int(mode) > 7:
                    return json.dumps({'Err': "Invalid value '%s' "
                                              "specified for fsMode. Please "
                                              "specify an octal value." %
                                              fs_mode_str})
        return fs_mode_str

    def _get_create_volume_args(self, options):
        ret_args = dict()
        ret_args['size'] = self._get_int_option(
            options, 'size', volume.DEFAULT_SIZE)
        ret_args['provisioning'] = self._get_str_option(
            options, 'provisioning', volume.DEFAULT_PROV,
            ['full', 'thin', 'dedup'])
        ret_args['flash-cache'] = self._get_str_option(
            options, 'flash-cache', volume.DEFAULT_FLASH_CACHE,
            ['true', 'false'])
        ret_args['qos-name'] = self._get_str_option(
            options, 'qos-name', volume.DEFAULT_QOS)
        ret_args['compression'] = self._get_str_option(
            options, 'compression', volume.DEFAULT_COMPRESSION_VAL,
            ['true', 'false'])
        ret_args['fsOwner'] = self._get_fs_owner(options)
        ret_args['fsMode'] = self._get_fs_mode(options)
        ret_args['mountConflictDelay'] = self._get_int_option(
            options, 'mountConflictDelay',
            volume.DEFAULT_MOUNT_CONFLICT_DELAY)
        ret_args['cpg'] = self._get_str_option(options, 'cpg', None)
        ret_args['snapcpg'] = self._get_str_option(options, 'snapcpg', None)
        ret_args['replicationGroup'] = self._get_str_option(
            options, 'replicationGroup', None)

        return ret_args

    def _validate_rcg_params(self, rcg_name, backend_name):
        LOG.info("Validating RCG: %s, backend name: %s..." % (rcg_name,
                                                              backend_name))
        hpepluginconfig = self._backend_configs[backend_name]
        replication_device = hpepluginconfig.replication_device

        LOG.info("Replication device: %s" % six.text_type(replication_device))

        if rcg_name and not replication_device:
            msg = "Request to create replicated volume cannot be fulfilled " \
                  "without defining 'replication_device' entry defined in " \
                  "hpe.conf for the backend '%s'. Please add it and execute " \
                  "the request again." % backend_name
            raise exception.InvalidInput(reason=msg)

        if replication_device and not rcg_name:
            backend_names = list(self._backend_configs.keys())
            backend_names.sort()

            msg = "'%s' is a replication enabled backend. " \
                  "Request to create replicated volume cannot be fulfilled " \
                  "without specifying 'replicationGroup' option in the " \
                  "request. Please either specify 'replicationGroup' or use " \
                  "a normal backend and execute the request again. List of " \
                  "backends defined in hpe.conf: %s" % (backend_name,
                                                        backend_names)
            raise exception.InvalidInput(reason=msg)

        if rcg_name and replication_device:

            def _check_valid_replication_mode(mode):
                valid_modes = ['synchronous', 'asynchronous', 'streaming']
                if mode.lower() not in valid_modes:
                    msg = "Unknown replication mode '%s' specified. Valid " \
                          "values are 'synchronous | asynchronous | " \
                          "streaming'" % mode
                    raise exception.InvalidInput(reason=msg)

            rep_mode = replication_device['replication_mode'].lower()
            _check_valid_replication_mode(rep_mode)
            if replication_device.get('quorum_witness_ip'):
                if rep_mode.lower() != 'synchronous':
                    msg = "For Peer Persistence, replication mode must be " \
                          "synchronous"
                    raise exception.InvalidInput(reason=msg)

            sync_period = replication_device.get('sync_period')
            if sync_period and rep_mode == 'synchronous':
                msg = "'sync_period' can be defined only for 'asynchronous'" \
                      " and 'streaming' replicate modes"
                raise exception.InvalidInput(reason=msg)

            if (rep_mode == 'asynchronous' or rep_mode == 'streaming')\
                    and sync_period:
                try:
                    sync_period = int(sync_period)
                except ValueError as ex:
                    msg = "Non-integer value '%s' not allowed for " \
                          "'sync_period'. %s" % (
                              replication_device.sync_period, ex)
                    raise exception.InvalidInput(reason=msg)
                else:
                    SYNC_PERIOD_LOW = 300
                    SYNC_PERIOD_HIGH = 31622400
                    if sync_period < SYNC_PERIOD_LOW or \
                       sync_period > SYNC_PERIOD_HIGH:
                        msg = "'sync_period' must be between 300 and " \
                              "31622400 seconds."
                        raise exception.InvalidInput(reason=msg)

    def _parse_help_opt(self, contents):
        valid_opts = ['help']
        self._validate_opts('display help', contents, valid_opts)
        return {'operation': 'create_help_content',
                '_vol_orchestrator': 'volume'}

    @staticmethod
    def _validate_name(vol_name):
        is_valid_name = re.match("^[A-Za-z0-9]+[A-Za-z0-9_-]+$", vol_name)
        if not is_valid_name:
            msg = 'Invalid volume name: %s is passed.' % vol_name
            raise exception.InvalidInput(reason=msg)
