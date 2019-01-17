from oslo_log import log as logging

from hpedockerplugin import exception
from hpedockerplugin import request_parser

LOG = logging.getLogger(__name__)


class RequestRouter(object):
    def __init__(self, **kwargs):
        self._orchestrators = {'volume': kwargs.get('vol_orchestrator'),
                               'file': kwargs.get('file_orchestrator')}
        all_configs = kwargs.get('all_configs')
        self._parser_factory = request_parser.RequestParserFactory(
            all_configs)

    def route_request(self, contents):
        req_parser = self._parser_factory.get_request_parser(contents)
        req_ctxt = req_parser.parse_request(contents)
        orchestrator_name = req_ctxt['orchestrator']
        orchestrator = self._orchestrators[orchestrator_name]
        if orchestrator:
            operation = req_ctxt['operation']
            kwargs = req_ctxt['kwargs']
            return getattr(self._orchestrators[orchestrator_name],
                           operation)(**kwargs)
        else:
            msg = "'%s' driver is not configured. Please refer to" \
                  "the document to learn about configuring the driver."
            LOG.error(msg)
            raise exception.InvalidInput(msg)
