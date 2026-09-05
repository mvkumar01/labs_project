"""Legacy configuration discovery. Live orders require assigned database routes."""
import contextlib
import os


class StaticOrderProxyRequired(RuntimeError):
    pass


def order_proxy_url():
    """Read legacy deployment configuration for operator migration tools only."""
    return next((os.environ[k].strip() for k in (
        'LIVE_ORDER_PROXY_URL','LIVE_OUTBOUND_PROXY_URL','QUOTAGUARDSTATIC_URL'
    ) if os.environ.get(k,'').strip()), '')


@contextlib.contextmanager
def order_proxy(sdk_client=None):
    raise StaticOrderProxyRequired('Legacy proxy context disabled; use the assigned order transport')
    yield  # pragma: no cover


def configure_outbound_proxy():
    """No process-wide proxy changes. Data requests retain direct egress."""
    return ''
