"""Putting the gateway together from its settings."""

from fastapi import FastAPI

from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID, DevAuthenticator
from factotum.model_gateway.api import create_app
from factotum.model_gateway.bindings import InMemoryBindingRepository, load_bindings_file
from factotum.model_gateway.core import ModelGateway
from factotum.model_gateway.secrets import EnvSecretResolver
from factotum.model_gateway.settings import GatewaySettings


def build_gateway(settings: GatewaySettings) -> ModelGateway:
    repository = InMemoryBindingRepository()
    if settings.gateway_bindings_file is not None:
        for binding in load_bindings_file(settings.gateway_bindings_file, tenant_id=DEV_TENANT_ID):
            repository.add(binding)
    return ModelGateway(repository, EnvSecretResolver())


def build_app(settings: GatewaySettings) -> FastAPI:
    """The gateway as a web application.

    Until the hub issues real credentials the only way to authenticate is the development
    authenticator, which refuses to exist outside development and test: in production this
    raises, and the service does not start.
    """
    authenticator = DevAuthenticator(settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
    return create_app(build_gateway(settings), authenticator, docs=not settings.is_production)
