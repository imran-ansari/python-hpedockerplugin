import uuid

DEFAULT_MOUNT_SHARE = "True"


def create_metadata(backend, fpg, vfs, fstore, share_name, vfs_ip,
                    share_dir=None, protocol='nfs', readonly=False,
                    proto_options=None, soft_quota=0, hard_quota=0,
                    allow_ips=None, deny_ips=None, comment=''):
    return {
        'backend': backend,
        'id': str(uuid.uuid4()),
        'fpg': fpg,
        'vfs': vfs,
        'fstore': fstore,
        'shareName': share_name,
        'vfsIP': vfs_ip,
        'shareDir': share_dir,
        'protocol': protocol,
        'readonly': readonly,
        'softQuota': soft_quota,
        'hardQuota': hard_quota,
        'allowIPs': allow_ips,
        'denyIPs': deny_ips,
        'protocolOpts': proto_options,
        'snapshots': [],
        'comment': comment,
    }
