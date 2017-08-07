# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import asyncio
import difflib
import inspect
import logging
import operator
import re
import sys

from collections import OrderedDict
from functools import wraps, partial
from types import MethodType

# from flask import url_for, request, current_app
# from flask import make_response as original_flask_make_response
# from flask.helpers import _endpoint_from_view_func
# from flask.signals import got_request_exception

from sanic.router import RouteExists, url_hash
from sanic.response import HTTPResponse, text, COMMON_STATUS_CODES, ALL_STATUS_CODES
from sanic.handlers import ErrorHandler
from sanic.exceptions import SanicException, InvalidUsage, NotFound
from sanic.server import CIDict
from sanic import exceptions, Blueprint


from jsonschema import RefResolver

# from werkzeug import cached_property
# from werkzeug.datastructures import Headers
# from werkzeug.exceptions import HTTPException, MethodNotAllowed, NotFound, NotAcceptable, InternalServerError
# from werkzeug.http import HTTP_STATUS_CODES
# from werkzeug.wrappers import BaseResponse

from . import apidoc
from .mask import ParseError, MaskError
from .namespace import Namespace
from .postman import PostmanCollectionV1
from .resource import Resource
from .swagger import Swagger
from .utils import default_id, camel_to_dash, unpack, best_match_accept_mimetype, get_accept_mimetypes
from .representations import output_json

RE_RULES = re.compile('(<.*>)')

# List headers that should never be handled by Flask-RESTPlus
HEADERS_BLACKLIST = ('Content-Length',)

DEFAULT_REPRESENTATIONS = [('application/json', output_json)]

log = logging.getLogger(__name__)


class Api(object):
    '''
    The main entry point for the application.
    You need to initialize it with a Flask Application: ::

    >>> app = Sanic(__name__)
    >>> api = Api(app)

    Alternatively, you can use :meth:`init_app` to set the Flask application
    after it has been constructed.

    The endpoint parameter prefix all views and resources:

        - The API root/documentation will be ``{endpoint}.root``
        - A resource registered as 'resource' will be available as ``{endpoint}.resource``

    :param sanic.Sanic|sanic.Blueprint app: the Flask application object or a Blueprint
    :param str version: The API version (used in Swagger documentation)
    :param str title: The API title (used in Swagger documentation)
    :param str description: The API description (used in Swagger documentation)
    :param str terms_url: The API terms page URL (used in Swagger documentation)
    :param str contact: A contact email for the API (used in Swagger documentation)
    :param str license: The license associated to the API (used in Swagger documentation)
    :param str license_url: The license page URL (used in Swagger documentation)
    :param str endpoint: The API base endpoint (default to 'api).
    :param str default: The default namespace base name (default to 'default')
    :param str default_label: The default namespace label (used in Swagger documentation)
    :param str default_mediatype: The default media type to return
    :param bool validate: Whether or not the API should perform input payload validation.
    :param str doc: The documentation path. If set to a false value, documentation is disabled.
                (Default to '/')
    :param list decorators: Decorators to attach to every resource
    :param bool catch_all_404s: Use :meth:`handle_error`
        to handle 404 errors throughout your app
    :param dict authorizations: A Swagger Authorizations declaration as dictionary
    :param bool serve_challenge_on_401: Serve basic authentication challenge with 401
        responses (default 'False')
    :param FormatChecker format_checker: A jsonschema.FormatChecker object that is hooked into
        the Model validator. A default or a custom FormatChecker can be provided (e.g., with custom
        checkers), otherwise the default action is to not enforce any format validation.
    '''

    uid_counter = 0
    def __init__(self, app=None, version='1.0', title=None, description=None,
            terms_url=None, license=None, license_url=None,
            contact=None, contact_url=None, contact_email=None,
            authorizations=None, security=None, doc='/', default_id=default_id,
            default='default', default_label='Default namespace', validate=None,
            tags=None, prefix='',
            default_mediatype='application/json', decorators=None,
            catch_all_404s=False, serve_challenge_on_401=False, format_checker=None,
            **kwargs):
        self.version = version
        self.title = title or 'API'
        self.description = description
        self.terms_url = terms_url
        self.contact = contact
        self.contact_email = contact_email
        self.contact_url = contact_url
        self.license = license
        self.license_url = license_url
        self.authorizations = authorizations
        self.security = security
        self.default_id = default_id
        self._validate = validate
        self._doc = doc
        self._doc_view = None
        self._default_error_handler = None
        self.tags = tags or []

        self.error_handlers = {
            ParseError: mask_parse_error_handler,
            MaskError: mask_error_handler,
        }
        self._schema = None
        self.models = {}
        self._refresolver = None
        self.format_checker = format_checker
        self.namespaces = []
        self.default_namespace = self.namespace(default, default_label,
            endpoint='{0}-declaration'.format(default),
            validate=validate,
            api=self,
            path='/',
        )
        self.ns_paths = dict()

        self.representations = OrderedDict(DEFAULT_REPRESENTATIONS)
        self.urls = {}
        self.prefix = prefix
        self.default_mediatype = default_mediatype
        self.decorators = decorators if decorators else []
        self.catch_all_404s = catch_all_404s
        self.serve_challenge_on_401 = serve_challenge_on_401
        self.blueprint_setup = None
        self.endpoints = set()
        self.resources = []
        self.app = None
        self.blueprint = None
        Api.uid_counter += 1
        self._uid = Api.uid_counter

        if app is not None:
            self.app = app
            self.init_app(app)
        # super(Api, self).__init__(app, **kwargs)

    def init_app(self, app, **kwargs):
        '''
        Allow to lazy register the API on a Sanic application::

        >>> app = Sanic(__name__)
        >>> api = Api()
        >>> api.init_app(app)

        :param sanic.Sanic app: the Flask application object
        :param str title: The API title (used in Swagger documentation)
        :param str description: The API description (used in Swagger documentation)
        :param str terms_url: The API terms page URL (used in Swagger documentation)
        :param str contact: A contact email for the API (used in Swagger documentation)
        :param str license: The license associated to the API (used in Swagger documentation)
        :param str license_url: The license page URL (used in Swagger documentation)

        '''
        if self.app is None:
            self.app = app
        self.title = kwargs.get('title', self.title)
        self.description = kwargs.get('description', self.description)
        self.terms_url = kwargs.get('terms_url', self.terms_url)
        self.contact = kwargs.get('contact', self.contact)
        self.contact_url = kwargs.get('contact_url', self.contact_url)
        self.contact_email = kwargs.get('contact_email', self.contact_email)
        self.license = kwargs.get('license', self.license)
        self.license_url = kwargs.get('license_url', self.license_url)
        self._add_specs = kwargs.get('add_specs', True)

        # If app is a blueprint, defer the initialization
        try:
            if isinstance(app, Blueprint):
                raise RuntimeError("As of Sanic 0.4.1, you cannot use Sanic restplus on a Blueprint. This will likely "
                                   "change in the future.")
            app.record(self._deferred_blueprint_init)
        # Flask.Blueprint has a 'record' attribute, Flask.Api does not
        except AttributeError:
            self._init_app(app)
        else:
            self.blueprint = app

    def _init_app(self, app):
        '''
        Perform initialization actions with the given :class:`sanic.Sanic` object.

        :param sanic.Sanic app: The flask application object
        '''
        self._register_specs(self.blueprint or app)
        self._register_doc(self.blueprint or app)

        #TODO Sanic fix exception handling
        app.error_handler = ApiErrorHandler(app.error_handler, self)
        #app.handle_user_exception = partial(self.error_router, app.handle_user_exception)

        if len(self.resources) > 0:
            for resource, urls, kwargs in self.resources:
                self._register_view(app, resource, *urls, **kwargs)

        self._register_apidoc(app)
        self._validate = self._validate if self._validate is not None else app.config.get('RESTPLUS_VALIDATE', False)
        app.config.setdefault('RESTPLUS_MASK_HEADER', 'X-Fields')
        app.config.setdefault('RESTPLUS_MASK_SWAGGER', True)

    def __getattr__(self, name):
        try:
            return getattr(self.default_namespace, name)
        except AttributeError:
            raise AttributeError('Api does not have {0} attribute'.format(name))

    def _complete_url(self, url_part, registration_prefix):
        '''
        This method is used to defer the construction of the final url in
        the case that the Api is created with a Blueprint.

        :param url_part: The part of the url the endpoint is registered with
        :param registration_prefix: The part of the url contributed by the
            blueprint.  Generally speaking, BlueprintSetupState.url_prefix
        '''
        parts = (registration_prefix, self.prefix, url_part)
        return ''.join(part for part in parts if part)

    def _register_apidoc(self, app):
        if not hasattr(app, 'extensions'):
            app.extensions = {}
        conf = app.extensions.setdefault('restplus', {})
        if not conf.get('apidoc_registered', False):
            app.blueprint(apidoc.apidoc)
        conf['apidoc_registered'] = True

    def _register_specs(self, app_or_blueprint):
        if self._add_specs:
            endpoint = str('specs') + str(self._uid)
            self._register_view(
                app_or_blueprint,
                SwaggerView,
                self.prefix + '/swagger.json',
                endpoint=endpoint,
                resource_class_args=(self, )
            )
            self.endpoints.add(endpoint)

    def _register_doc(self, app_or_blueprint):
        root_path = self.prefix or '/'
        if self._add_specs and self._doc:
            # app_or_blueprint.add_url_rule(self._doc, 'doc', self.render_doc)
            app_or_blueprint.add_route(named_route_fn('doc'+str(self._uid), self.render_doc), self._doc)

        if self._doc != root_path:
            try:# app_or_blueprint.add_url_rule(self.prefix or '/', 'root', self.render_root)
                app_or_blueprint.add_route(named_route_fn('root'+str(self._uid), self.render_root), root_path)

            except RouteExists:
                pass




    def register_resource(self, namespace, resource, *urls, **kwargs):
        endpoint = kwargs.pop('endpoint', None)
        endpoint = str(endpoint or self.default_endpoint(resource, namespace))

        kwargs['endpoint'] = endpoint
        self.endpoints.add(endpoint)

        if self.app is not None:
            self._register_view(self.app, resource, *urls, **kwargs)
        else:
            self.resources.append((resource, urls, kwargs))
        return endpoint

    def _register_view(self, app, resource, *urls, **kwargs):
        endpoint = kwargs.pop('endpoint', None) or camel_to_dash(resource.__name__)
        resource_class_args = kwargs.pop('resource_class_args', ())
        resource_class_kwargs = kwargs.pop('resource_class_kwargs', {})

        # NOTE: 'view_functions' is cleaned up from Blueprint class in Flask 1.0
        if endpoint in getattr(app, 'view_functions', {}):
            previous_view_class = app.view_functions[endpoint].__dict__['view_class']

            # if you override the endpoint with a different class, avoid the collision by raising an exception
            if previous_view_class != resource:
                msg = 'This endpoint (%s) is already set to the class %s.' % (endpoint, previous_view_class.__name__)
                raise ValueError(msg)

        resource.mediatypes = self.mediatypes_method()  # Hacky
        resource.endpoint = endpoint
        resource_func = self.output(resource.as_view(self, *resource_class_args,
            **resource_class_kwargs))
        # hacky, we want to change the __name__ of this func to `endpoint` so it can be found with url_for.
        resource_func.__name__ = endpoint
        for decorator in self.decorators:
            resource_func = decorator(resource_func)

        for url in urls:
            # If this Api has a blueprint
            if self.blueprint:
                # And this Api has been setup
                if self.blueprint_setup:
                    # Set the rule to a string directly, as the blueprint is already
                    # set up.
                    self.blueprint_setup.add_url_rule(url, view_func=resource_func, **kwargs)
                    continue
                else:
                    # Set the rule to a function that expects the blueprint prefix
                    # to construct the final url.  Allows deferment of url finalization
                    # in the case that the associated Blueprint has not yet been
                    # registered to an application, so we can wait for the registration
                    # prefix
                    rule = partial(self._complete_url, url)
            else:
                # If we've got no Blueprint, just build a url with no prefix
                rule = self._complete_url(url, '')
            # Add the url to the application or blueprint
            app.add_route(resource_func, rule)

    def output(self, resource):
        '''
        Wraps a resource (as a flask view function),
        for cases where the resource does not directly return a response object

        :param resource: The resource as a flask view function
        '''
        @wraps(resource)
        async def wrapper(request, *args, **kwargs):
            resp = resource(request, *args, **kwargs)
            while asyncio.iscoroutine(resp):
                resp = await resp
            if isinstance(resp, HTTPResponse):
                return resp
            data, code, headers = unpack(resp)
            return self.make_response(request, data, code, headers=headers)
        return wrapper

    def make_response(self, request, data, *args, **kwargs):
        '''
        Looks up the representation transformer for the requested media
        type, invoking the transformer to create a response object. This
        defaults to default_mediatype if no transformer is found for the
        requested mediatype. If default_mediatype is None, a 406 Not
        Acceptable response will be sent as per RFC 2616 section 14.1

        :param data: Python object containing response data to be transformed
        '''
        default_mediatype = kwargs.pop('fallback_mediatype', None) or self.default_mediatype
        mediatype = best_match_accept_mimetype(request,
            self.representations,
            default=default_mediatype,
        )
        if mediatype is None:
            raise exceptions.SanicException("Not Acceptable", 406)
        if mediatype in self.representations:
            resp = self.representations[mediatype](request, data, *args, **kwargs)
            resp.headers['Content-Type'] = mediatype
            return resp
        elif mediatype == 'text/plain':
            resp = text(str(data), *args, **kwargs)
            resp.headers['Content-Type'] = 'text/plain'
            return resp
        else:
            raise exceptions.ServerError(None)

    def documentation(self, func):
        '''A decorator to specify a view function for the documentation'''
        self._doc_view = func
        return func

    def render_root(self, request):
        self.abort(404)

    async def render_doc(self, request):
        '''Override this method to customize the documentation page'''
        if self._doc_view:
            return self._doc_view()
        elif not self._doc:
            self.abort(404)
        response = apidoc.ui_for(request, self)
        if asyncio.iscoroutine(response):
            response = await response
        return response

    def default_endpoint(self, resource, namespace):
        '''
        Provide a default endpoint for a resource on a given namespace.

        Endpoints are ensured not to collide.

        Override this method specify a custom algoryhtm for default endpoint.

        :param Resource resource: the resource for which we want an endpoint
        :param Namespace namespace: the namespace holding the resource
        :returns str: An endpoint name
        '''
        endpoint = camel_to_dash(resource.__name__)
        if namespace is not self.default_namespace:
            endpoint = '{ns.name}_{endpoint}'.format(ns=namespace, endpoint=endpoint)
        if endpoint in self.endpoints:
            suffix = 2
            while True:
                new_endpoint = '{base}_{suffix}'.format(base=endpoint, suffix=suffix)
                if new_endpoint not in self.endpoints:
                    endpoint = new_endpoint
                    break
                suffix += 1
        return endpoint

    def get_ns_path(self, ns):
        return self.ns_paths.get(ns)

    def ns_urls(self, ns, urls):
        path = self.get_ns_path(ns) or ns.path
        return [path + url for url in urls]

    def add_namespace(self, ns, path=None):
        '''
        This method registers resources from namespace for current instance of api.
        You can use argument path for definition custom prefix url for namespace.

        :param Namespace ns: the namespace
        :param path: registration prefix of namespace
        '''
        if ns not in self.namespaces:
            self.namespaces.append(ns)
            if self not in ns.apis:
                ns.apis.append(self)
            # Associate ns with prefix-path
            if path is not None:
                self.ns_paths[ns] = path
        # Register resources
        for resource, urls, kwargs in ns.resources:
            self.register_resource(ns, resource, *self.ns_urls(ns, urls), **kwargs)
        # Register models
        for name, definition in ns.models.items():
            self.models[name] = definition
        # Register error handlers
        for exception, handler in ns.error_handlers.items():
            self.error_handlers[exception] = handler

    def namespace(self, *args, **kwargs):
        '''
        A namespace factory.

        :returns Namespace: a new namespace instance
        '''
        ns = Namespace(*args, **kwargs)
        self.add_namespace(ns)
        return ns

    def endpoint(self, name):
        if self.blueprint:
            return '{0}.{1}'.format(self.blueprint.name, name)
        else:
            return name

    @property
    def specs_url(self):
        '''
        The Swagger specifications absolute url (ie. `swagger.json`)

        :rtype: str
        '''
        try:
            specs_url = self.app.url_for(self.endpoint('specs'+str(self._uid)), _external=True)
        except (AttributeError, KeyError):
            raise RuntimeError("The API object does not have an `app` assigned.")
        return specs_url
    @property
    def base_url(self):
        '''
        The API base absolute url

        :rtype: str
        '''
        root_path = self.prefix or '/'
        try:
            if self._doc == root_path:
                base_url = self.app.url_for(self.endpoint('doc'+str(self._uid)), _external=True)
            else:
                base_url = self.app.url_for(self.endpoint('root'+str(self._uid)), _external=True)
        except (AttributeError, KeyError):
            raise RuntimeError("The API object does not have an `app` assigned.")
        return base_url


    @property
    def base_path(self):
        '''
        The API path

        :rtype: str
        '''
        root_path = self.prefix or '/'
        try:
            if self._doc == root_path:
                base_url = self.app.url_for(self.endpoint('doc'+str(self._uid)))
            else:
                base_url = self.app.url_for(self.endpoint('root'+str(self._uid)))
        except (AttributeError, KeyError):
            raise RuntimeError("The API object does not have an `app` assigned.")
        return base_url

    #@cached_property
    @property
    def __schema__(self):
        '''
        The Swagger specifications/schema for this API

        :returns dict: the schema as a serializable dict
        '''
        if not self._schema:
            try:
                self._schema = Swagger(self).as_dict()
            except Exception:
                # Log the source exception for debugging purpose
                # and return an error message
                msg = 'Unable to render schema'
                log.exception(msg)  # This will provide a full traceback
                return {'error': msg}
        return self._schema

    def errorhandler(self, exception):
        '''A decorator to register an error handler for a given exception'''
        if inspect.isclass(exception) and issubclass(exception, Exception):
            # Register an error handler for a given exception
            def wrapper(func):
                self.error_handlers[exception] = func
                return func
            return wrapper
        else:
            # Register the default error handler
            self._default_error_handler = exception
            return exception

    def owns_endpoint(self, endpoint):
        '''
        Tests if an endpoint name (not path) belongs to this Api.
        Takes into account the Blueprint name part of the endpoint name.

        :param str endpoint: The name of the endpoint being checked
        :return: bool
        '''

        if self.blueprint:
            if endpoint.startswith(self.blueprint.name):
                endpoint = endpoint.split(self.blueprint.name + '.', 1)[-1]
            else:
                return False
        return endpoint in self.endpoints

    @staticmethod
    def _dummy_router_get(router, method, request):
        url = request.path
        route = router.routes_static.get(url)
        method_not_supported = InvalidUsage(
            'Method {} not allowed for URL {}'.format(
                method, url), status_code=405)
        if route:
            if route.methods and method not in route.methods:
                method_not_supported.valid_methods = route.methods
                raise method_not_supported
            match = route.pattern.match(url)
        else:
            route_found = False
            # Move on to testing all regex routes
            for route in router.routes_dynamic[url_hash(url)]:
                match = route.pattern.match(url)
                route_found |= match is not None
                # Do early method checking
                if match and method in route.methods:
                    break
            else:
                # Lastly, check against all regex routes that cannot be hashed
                for route in router.routes_always_check:
                    match = route.pattern.match(url)
                    route_found |= match is not None
                    # Do early method checking
                    if match and method in route.methods:
                        break
                else:
                    # Route was found but the methods didn't match
                    if route_found:
                        method_not_supported.valid_methods = route.methods
                        raise method_not_supported
                    raise NotFound('Requested URL {} not found'.format(url))

        return route

    def _should_use_fr_error_handler(self, request):
        '''
        Determine if error should be handled with Sanic-Restplus or default Sanic

        The goal is to return Sanic error handlers for non-SR-related routes,
        and SR errors (with the correct media type) for SR endpoints. This
        method currently handles 404 and 405 errors.

        :return: bool
        '''
        if request is None:
            # This must be a Sanic error if request is None.
            return False
        try:
            app = request.app
        except AttributeError:
            # if request doesn't have .app, then it is also a Sanic error
            return False
        try:
            return self._dummy_router_get(app.router, request.method, request)
        except InvalidUsage as e:
            # Check if the other HTTP methods at this url would hit the Api
            try:
                try_route_method = next(iter(e.valid_methods))
            except (AttributeError, KeyError):
                if request.method == "GET":
                    try_route_method = "POST"
                else:
                    try_route_method = "GET"
            route = self._dummy_router_get(app.router, try_route_method, request)
            return self.owns_endpoint(route.name)
        except NotFound:
            return self.catch_all_404s
        except:
            # Other stuff throws other kinds of exceptions, such as Redirect
            pass

    def _has_fr_route(self, request):
        '''Encapsulating the rules for whether the request was to a Flask endpoint'''
        # 404's, 405's, which might not have a url_rule
        route = self._should_use_fr_error_handler(request)
        if route is True:
            return True
        # for all other errors, just check if FR dispatched the route
        if not route or not route.handler or not route.name:
            return False
        return self.owns_endpoint(route.name)


    def handle_error(self, request, e):
        '''
        Error handler for the API transforms a raised exception into a Flask response,
        with the appropriate HTTP status code and body.

        :param Exception e: the raised Exception object

        '''
        # todo: sanic: wtf is this?
        #got_request_exception.send(current_app._get_current_object(), exception=e)
        app = request.app
        headers = CIDict()
        if e.__class__ in self.error_handlers:
            handler = self.error_handlers[e.__class__]
            result = handler(e)
            default_data, code, headers = unpack(result, 500)
        elif isinstance(e, SanicException):
            code = e.status_code
            status = COMMON_STATUS_CODES.get(code)
            if not status:
                status = ALL_STATUS_CODES.get(code)
            if status and isinstance(status, bytes):
                status = status.decode('ascii')
            default_data = {
                'message': getattr(e, 'message', status)
            }
            # headers = e.get_response().headers
        elif self._default_error_handler:
            result = self._default_error_handler(e)
            default_data, code, headers = unpack(result, 500)
        else:
            code = 500
            status = COMMON_STATUS_CODES.get(code, str(e))
            if status and isinstance(status, bytes):
                status = status.decode('ascii')
            default_data = {
                'message': status,
            }

        default_data['message'] = default_data.get('message', str(e))
        data = getattr(e, 'data', default_data)
        fallback_mediatype = None

        if code >= 500:
            exc_info = sys.exc_info()
            if exc_info[1] is None:
                exc_info = None
            #current_app.log_exception(exc_info)

        elif code == 404 and app.config.get("ERROR_404_HELP", True):
            data['message'] = self._help_on_404(request, data.get('message', None))

        elif code == 406 and self.default_mediatype is None:
            # if we are handling NotAcceptable (406), make sure that
            # make_response uses a representation we support as the
            # default mediatype (so that make_response doesn't throw
            # another NotAcceptable error).
            supported_mediatypes = list(self.representations.keys())
            fallback_mediatype = supported_mediatypes[0] if supported_mediatypes else "text/plain"

        # Remove blacklisted headers
        for header in HEADERS_BLACKLIST:
            headers.pop(header, None)

        resp = self.make_response(request, data, code, headers, fallback_mediatype=fallback_mediatype)

        if code == 401:
            resp = self.unauthorized(resp)
        return resp

    def _help_on_404(self, request, message=None):
        raise NotImplementedError("Help on 404 is not yet implemented for Sanic-RestPlus")
        rules = dict([(RE_RULES.sub('', rule.rule), rule.rule)
                      for rule in current_app.url_map.iter_rules()])
        close_matches = difflib.get_close_matches(request.path, rules.keys())
        if close_matches:
            # If we already have a message, add punctuation and continue it.
            message = ''.join((
                (message.rstrip('.') + '. ') if message else '',
                'You have requested this URI [',
                request.path,
                '] but did you mean ',
                ' or '.join((rules[match] for match in close_matches)),
                ' ?',
            ))
        return message

    def as_postman(self, urlvars=False, swagger=False):
        '''
        Serialize the API as Postman collection (v1)

        :param bool urlvars: whether to include or not placeholders for query strings
        :param bool swagger: whether to include or not the swagger.json specifications

        '''
        return PostmanCollectionV1(self, swagger=swagger).as_dict(urlvars=urlvars)

    # TODO: Sanic, payload (as a property) cannot see the request.
    #@property
    def payload(self, request):
        '''Store the input payload in the current request context'''
        return request.json

    @property
    def refresolver(self):
        if not self._refresolver:
            self._refresolver = RefResolver.from_schema(self.__schema__)
        return self._refresolver

    @staticmethod
    def _blueprint_setup_add_url_rule_patch(blueprint_setup, rule, endpoint=None, view_func=None, **options):
        '''
        Method used to patch BlueprintSetupState.add_url_rule for setup
        state instance corresponding to this Api instance.  Exists primarily
        to enable _complete_url's function.

        :param blueprint_setup: The BlueprintSetupState instance (self)
        :param rule: A string or callable that takes a string and returns a
            string(_complete_url) that is the url rule for the endpoint
            being registered
        :param endpoint: See BlueprintSetupState.add_url_rule
        :param view_func: See BlueprintSetupState.add_url_rule
        :param **options: See BlueprintSetupState.add_url_rule
        '''

        if callable(rule):
            rule = rule(blueprint_setup.url_prefix)
        elif blueprint_setup.url_prefix:
            rule = blueprint_setup.url_prefix + rule
        options.setdefault('subdomain', blueprint_setup.subdomain)
        if endpoint is None:
            endpoint = _endpoint_from_view_func(view_func)
        defaults = blueprint_setup.url_defaults
        if 'defaults' in options:
            defaults = dict(defaults, **options.pop('defaults'))
        blueprint_setup.app.add_url_rule(rule, '%s.%s' % (blueprint_setup.blueprint.name, endpoint),
                                         view_func, defaults=defaults, **options)

    def _deferred_blueprint_init(self, setup_state):
        '''
        Synchronize prefix between blueprint/api and registration options, then
        perform initialization with setup_state.app :class:`sanic.Sanic` object.
        When a :class:`flask_restplus.Api` object is initialized with a blueprint,
        this method is recorded on the blueprint to be run when the blueprint is later
        registered to a :class:`sanic.Sanic` object.  This method also monkeypatches
        BlueprintSetupState.add_url_rule with _blueprint_setup_add_url_rule_patch.

        :param setup_state: The setup state object passed to deferred functions
            during blueprint registration
        :type setup_state: flask.blueprints.BlueprintSetupState

        '''

        self.blueprint_setup = setup_state
        if setup_state.add_url_rule.__name__ != '_blueprint_setup_add_url_rule_patch':
            setup_state._original_add_url_rule = setup_state.add_url_rule
            setup_state.add_url_rule = MethodType(Api._blueprint_setup_add_url_rule_patch,
                                                  setup_state)
        if not setup_state.first_registration:
            raise ValueError('flask-restplus blueprints can only be registered once.')
        self._init_app(setup_state.app)

    def mediatypes_method(self):
        '''Return a method that returns a list of mediatypes'''
        return lambda resource_cls, request:\
            self.mediatypes(request) + [self.default_mediatype]

    def mediatypes(self, request):
        '''Returns a list of requested mediatypes sent in the Accept header'''
        return [h for h, q in sorted(get_accept_mimetypes(request),
                                     key=operator.itemgetter(1), reverse=True)]

    def representation(self, mediatype):
        '''
        Allows additional representation transformers to be declared for the
        api. Transformers are functions that must be decorated with this
        method, passing the mediatype the transformer represents. Three
        arguments are passed to the transformer:

        * The data to be represented in the response body
        * The http status code
        * A dictionary of headers

        The transformer should convert the data appropriately for the mediatype
        and return a Flask response object.

        Ex::

            @api.representation('application/xml')
            def xml(data, code, headers):
                resp = make_response(convert_data_to_xml(data), code)
                resp.headers.extend(headers)
                return resp
        '''
        def wrapper(func):
            self.representations[mediatype] = func
            return func
        return wrapper

    def unauthorized(self, response):
        '''Given a response, change it to ask for credentials'''

        if self.serve_challenge_on_401:
            realm = current_app.config.get("HTTP_BASIC_AUTH_REALM", "flask-restplus")
            challenge = u"{0} realm=\"{1}\"".format("Basic", realm)

            response.headers['WWW-Authenticate'] = challenge
        return response

    def url_for(self, resource, **values):
        '''
        Generates a URL to the given resource.

        Works like :func:`flask.url_for`.
        '''
        endpoint = resource.endpoint
        if self.blueprint:
            endpoint = '{0}.{1}'.format(self.blueprint.name, endpoint)
        return self.app.url_for(endpoint, **values)


class ApiErrorHandler(ErrorHandler):
    def __init__(self, original_handler, api):
        super(ApiErrorHandler, self).__init__()
        self.original_handler = original_handler
        self.api = api

    def response(self, request, e):
        '''
        This function decides whether the error occurred in a sanic-restplus
        endpoint or not. If it happened in a sanic-restplus endpoint, our
        handler will be dispatched. If it happened in an unrelated view, the
        app's original error handler will be dispatched.
        In the event that the error occurred in a sanic-restplus endpoint but
        the local handler can't resolve the situation, the router will fall
        back onto the original_handler as last resort.

        :param Exception e: the exception raised while handling the request
        '''
        if self.api._has_fr_route(request):
            try:
                return self.api.handle_error(request, e)
            except Exception as e:
                pass  # Fall through to original handler
        return self.original_handler.response(request, e)


class SwaggerView(Resource):
    '''Render the Swagger specifications as JSON'''
    def get(self, request):
        schema = self.api.__schema__
        return schema, 500 if 'error' in schema else 200

    def mediatypes(self):
        return ['application/json']

class named_route_fn(object):
    __slots__ = ['__name', 'fn']

    def __init__(self, name, fn):
        self.__name = name
        self.fn = fn

    @property
    def __name__(self):
        return self.__name

    @__name__.setter
    def __name__(self, val):
        self.__name = val

    def __call__(self, *args, **kwargs):
        return self.fn(*args, **kwargs)

def mask_parse_error_handler(error):
    '''When a mask can't be parsed'''
    return {'message': 'Mask parse error: {0}'.format(error)}, 400


def mask_error_handler(error):
    '''When any error occurs on mask'''
    return {'message': 'Mask error: {0}'.format(error)}, 400
