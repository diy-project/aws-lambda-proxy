#!/usr/bin/env python

import argparse
import logging
import sys
import time

from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
from fake_useragent import UserAgent
from SocketServer import ThreadingMixIn
from termcolor import colored

from lib.headers import FILTERED_REQUEST_HEADERS, FILTERED_RESPONSE_HEADERS,\
    DEFAULT_USER_AGENT
from lib.proxy import ProxyInstance
from lib.proxies.local import LocalProxy
from lib.proxies.aws import ShortLivedLambdaProxy, LongLivedLambdaProxy,\
    HybridLambdaProxy
from lib.proxies.mitm import MitmHttpsProxy
from lib.stats import Stats, ProxyStatsModel

logging.basicConfig(filename='main.log', filemode='w', level=logging.INFO)
logger = logging.getLogger('main')
logging.getLogger(
    'botocore.vendored.requests.packages.urllib3.connectionpool'
).setLevel(logging.ERROR)

DEFAULT_PORT = 1080
DEFAULT_MAX_LAMBDAS = 10

MITM_CERT_PATH = 'mitm.ca.pem'
MITM_KEY_PATH = 'mitm.key.pem'

OVERRIDE_USER_AGENT = False


def get_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', '-p', type=int, default=DEFAULT_PORT,
                        help='Port to listen on')
    parser.add_argument('--host', type=str, default='localhost',
                        help='Address to bind to')
    parser.add_argument('--local', '-l', action='store_true',
                        dest='runLocal',
                        help='Run the proxy locally')
    parser.add_argument('--function', '-f', dest='functions',
                        action='append', default=['simple-http-proxy'],
                        help='Lambda functions by name or ARN')

    parser.add_argument('--lambda-type', '-t', dest='lambdaType',
                        choices=['short', 'long', 'hybrid'],
                        default='hybrid', type=str,
                        help='Type of lambda workers to use')

    parser.add_argument('--large-transport', '-xl', dest='largeTransport',
                        choices=['s3', 'sqs'], default='sqs', type=str,
                        help='Option for long-lived lambdas to return '
                             'messages larger than the SQS payload')
    parser.add_argument('--s3-bucket', '-s3', dest='s3Bucket', type=str,
                        help='s3Bucket to use for large file transport')

    parser.add_argument('--max-lambdas', '-j', type=int,
                        default=DEFAULT_MAX_LAMBDAS, dest='maxLambdas',
                        help='Max number of lambdas running at any time')
    parser.add_argument('--enable-mitm', '-m', action='store_true',
                        dest='enableMitm',
                        help='Run as a MITM for TLS traffic')
    parser.add_argument('--verbose', '-v', action='store_true')
    return parser.parse_args()


def build_local_proxy(args, stats):
    """Request the resource locally"""

    print '  Running the proxy locally. This provides no privacy!'

    localProxy = LocalProxy(stats=stats)
    if args.enableMitm:
        print '  MITM proxy enabled'
        mitmProxy = MitmHttpsProxy(localProxy,
                                   certfile=MITM_CERT_PATH,
                                   keyfile=MITM_KEY_PATH,
                                   stats=stats,
                                   overrideUserAgent=OVERRIDE_USER_AGENT,
                                   verbose=args.verbose)
        return ProxyInstance(requestProxy=localProxy, streamProxy=mitmProxy)
    else:
        return ProxyInstance(requestProxy=localProxy, streamProxy=localProxy)


def build_lambda_proxy(args, stats):
    """Request the resource using lambda"""
    functions = args.functions
    lambdaType = args.lambdaType
    maxLambdas = args.maxLambdas
    s3Bucket = args.s3Bucket
    verbose = args.verbose

    print '  Running the proxy with lambda'
    if not functions:
        print 'No functions specified'
        sys.exit(-1)

    print '  Using functions:', ', '.join(functions)

    if lambdaType == 'short':
        print '  Using short-lived lambdas'
        lambdaProxy = ShortLivedLambdaProxy(functions=functions,
                                            maxParallelRequests=maxLambdas,
                                            s3Bucket=s3Bucket,
                                            stats=stats)
    elif lambdaType == 'long':
        print '  Using long-lived lambdas'
        lambdaProxy = LongLivedLambdaProxy(functions=functions,
                                           maxLambdas=maxLambdas,
                                           s3Bucket=s3Bucket,
                                           stats=stats,
                                           verbose=verbose)
    else:
        print '  Using hybrid lambdas'
        lambdaProxy = HybridLambdaProxy(functions=functions,
                                        maxLambdas=maxLambdas,
                                        s3Bucket=s3Bucket,
                                        stats=stats,
                                        verbose=verbose)

    if args.enableMitm:
        mitmProxy = MitmHttpsProxy(lambdaProxy,
                                   certfile=MITM_CERT_PATH,
                                   keyfile=MITM_KEY_PATH,
                                   stats=stats,
                                   overrideUserAgent=OVERRIDE_USER_AGENT,
                                   verbose=verbose)
        return ProxyInstance(requestProxy=lambdaProxy, streamProxy=mitmProxy)
    else:
        print '  HTTPS will use the local proxy'
        localProxy = LocalProxy(stats=stats)
        return ProxyInstance(requestProxy=lambdaProxy, streamProxy=localProxy)


def build_handler(proxy, stats, verbose):
    """Construct a request handler"""
    if UserAgent:
        ua = UserAgent()
        get_user_agent = lambda: ua.random
    else:
        get_user_agent = lambda: DEFAULT_USER_AGENT

    proxyStats = stats.get_model('proxy')

    def log_request_delay(function):
        def wrapper(*args, **kwargs):
            with proxyStats.record_delay():
                function(*args, **kwargs)
        return wrapper

    handlerLogger = logging.getLogger('handler')

    class ProxyHandler(BaseHTTPRequestHandler):

        def _print_request(self):
            print colored('command (http): %s %s' % (self.command, self.path),
                          'white', 'on_blue')
            for header in self.headers:
                print '  %s: %s' % (header, self.headers[header])

        def _print_response(self, response):
            print colored('url: %s' % self.path, 'white', 'on_yellow')
            print 'status:', response.statusCode
            for header in response.headers:
                print '  %s: %s' % (header, response.headers[header])
            print 'content-len:', len(response.content)

        def log_message(self, format, *args):
            handlerLogger.info('%s - [%s] %s' %
                               (self.client_address[0],
                                self.log_date_time_string(),
                                format % args))

        @log_request_delay
        def _proxy_request(self):
            if verbose: self._print_request()

            method = self.command.upper()
            url = self.path
            headers = {}

            # Approximate the length of the request
            approxRequestLen = 2 +  len(url) + len(method) + \
                               len(self.version_string())

            for header in self.headers:
                value = self.headers[header]
                approxRequestLen += len(header) + len(str(value)) + 4
                if header in FILTERED_REQUEST_HEADERS:
                    continue
                headers[header] = self.headers[header]
            headers['Connection'] = 'close'
            if OVERRIDE_USER_AGENT:
                headers['User-Agent'] = get_user_agent()

            # TODO: which other requests have no bodies?
            if method != 'GET':
                requestBody = self.rfile.read()
                approxRequestLen += len(requestBody)
            else:
                requestBody = None

            proxyStats.record_bytes_up(approxRequestLen)

            response = proxy.request(method, url, headers, requestBody)
            if verbose: self._print_response(response)

            # Approximate the response length
            approxResponseLen = 20

            self.send_response(response.statusCode)
            for header, value in response.headers.iteritems():
                if header in FILTERED_RESPONSE_HEADERS:
                    continue
                approxResponseLen = len(header) + len(str(value)) + 4
                self.send_header(header, value)
            self.send_header('Proxy-Connection', 'close')
            self.end_headers()
            self.wfile.write(response.content)
            approxResponseLen += len(response.content)
            proxyStats.record_bytes_down(approxResponseLen)
            return

        def _connect_request(self):
            if verbose: self._print_request()

            host, port = self.path.split(':')
            try:
                sock = proxy.connect(host, port)
            except Exception, e:
                logger.exception(e)
                self.send_error(520)
                self.end_headers()
                return

            try:
                self.send_response(200)
                self.send_header('Proxy-Agent', self.version_string())
                self.send_header('Proxy-Connection', 'close')
                self.end_headers()
                proxy.stream(self.connection, sock)
            except Exception, e:
                logger.exception(e)
                raise
            finally:
                sock.close()
            return

        do_GET = _proxy_request
        do_POST = _proxy_request
        do_HEAD = _proxy_request
        do_DELETE = _proxy_request
        do_PUT = _proxy_request
        do_PATCH = _proxy_request
        do_OPTIONS = _proxy_request
        do_CONNECT = _connect_request

    return ProxyHandler


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""


def main(host, port, args=None):
    stats = Stats()
    stats.register_model('proxy', ProxyStatsModel())

    print 'Configuring proxy'
    if args.runLocal:
        proxy = build_local_proxy(args, stats)
    else:
        proxy = build_lambda_proxy(args, stats)

    handler = build_handler(proxy, stats, verbose=args.verbose)
    server = ThreadedHTTPServer((host, port), handler)
    print 'Starting proxy, use <Ctrl-C> to stop'
    stats.start_live_summary()
    server.serve_forever()


if __name__ == '__main__':
    args = get_args()
    main(args.host, args.port, args)
