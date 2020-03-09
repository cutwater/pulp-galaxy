import json
import logging
from urllib import parse as urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import StreamingHttpResponse, HttpResponseRedirect
from django.urls import reverse

from rest_framework.exceptions import APIException, NotFound
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response

from pulpcore import client as bindings_client
import requests

from pulp_galaxy.app.api import base as api_base
from pulp_galaxy.app.api.ui import serializers
from pulp_galaxy.app.api.v3.serializers import CollectionSerializer, CollectionUploadSerializer
from pulp_galaxy.app.common import pulp
from pulp_galaxy.app.common import metrics
from pulp_galaxy.app.api import permissions
from pulp_galaxy.app import models
from pulp_galaxy.app import constants


log = logging.getLogger(__name__)


class CollectionViewSet(api_base.GenericViewSet):
    permission_classes = api_base.GALAXY_PERMISSION_CLASSES + [
        permissions.IsNamespaceOwnerOrPartnerEngineer,
    ]
    serializer_class = CollectionSerializer

    def list(self, request, *args, **kwargs):
        self.paginator.init_from_request(request)

        params = self.request.query_params.dict()
        params.update({
            'offset': self.paginator.offset,
            'limit': self.paginator.limit,
        })

        api = bindings_client.pulp_galaxy.GalaxyCollectionsApi(pulp.get_client())
        response = api.list(prefix=settings.X_PULP_API_PREFIX, **params)
        data = list(map(self._fix_item_urls, response.results))
        return self.paginator.paginate_proxy_response(data, response.count)

    def retrieve(self, request, *args, **kwargs):
        api = bindings_client.pulp_galaxy.GalaxyCollectionsApi(pulp.get_client())

        response = api.get(
            prefix=settings.X_PULP_API_PREFIX,
            namespace=self.kwargs['namespace'],
            name=self.kwargs['name']
        )
        response = self._fix_item_urls(response)

        return Response(response)

    def update(self, request, *args, **kwargs):
        namespace = self.kwargs['namespace']
        name = self.kwargs['name']

        namespace_obj = get_object_or_404(models.Namespace, name=namespace)
        self.check_object_permissions(self.request, namespace_obj)

        serializer = CollectionSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        collection = bindings_client.pulp_galaxy.models.Collection(deprecated=data.get('deprecated', False))

        api = bindings_client.pulp_galaxy.GalaxyCollectionsApi(pulp.get_client())

        response = api.put(
            prefix=settings.X_PULP_API_PREFIX,
            namespace=namespace,
            name=name,
            collection=collection,
        )

        return Response(response.to_dict())

    @staticmethod
    def _fix_item_urls(data):
        namespace = data['namespace']
        name = data['name']
        highest_version = data['highest_version']['version']

        data['href'] = reverse(
            'galaxy:api:v3:collection',
            kwargs=dict(namespace=namespace, name=name)
        )
        data['versions_url'] = reverse(
            'galaxy:api:v3:collection-version-list',
            kwargs=dict(namespace=namespace, name=name)
        )
        data['highest_version']['href'] = reverse(
            'galaxy:api:v3:collection-version',
            kwargs=dict(namespace=namespace, name=name, version=highest_version)
        )
        return data


class CollectionVersionViewSet(api_base.GenericViewSet):
    serializer_class = serializers.CollectionVersionSerializer

    def list(self, request, *args, **kwargs):
        self.paginator.init_from_request(request)

        params = self.request.query_params.dict()
        params.update({
            'offset': self.paginator.offset,
            'limit': self.paginator.limit,
            'certification': constants.CertificationStatus.CERTIFIED.value
        })

        api = bindings_client.pulp_galaxy.GalaxyCollectionVersionsApi(pulp.get_client())
        response = api.list(
            prefix=settings.X_PULP_API_PREFIX,
            namespace=self.kwargs['namespace'],
            name=self.kwargs['name'],
            **params,
        )

        # Consider an empty list of versions as a 404 on the Collection
        if not response.results:
            raise NotFound()

        self._fix_list_urls(response.results)
        return self.paginator.paginate_proxy_response(response.results, response.count)

    def retrieve(self, request, *args, **kwargs):
        api = bindings_client.pulp_galaxy.GalaxyCollectionVersionsApi(pulp.get_client())
        response = api.get(
            prefix=settings.X_PULP_API_PREFIX,
            namespace=self.kwargs['namespace'],
            name=self.kwargs['name'],
            version=self.kwargs['version'],
        )
        self._fix_retrieve_url(response)
        response['download_url'] = self._transform_pulp_url(request, response['download_url'])
        return Response(response)

    @staticmethod
    def _transform_pulp_url(request, pulp_url):
        """Translate URL returned by Pulp."""
        urlparts = urlparse.urlsplit(pulp_url)
        # Build relative URL by stripping scheme and netloc
        relative_url = urlparse.urlunsplit(('', '') + urlparts[2:])
        return request.build_absolute_uri(relative_url)

    def _fix_list_urls(self, data):
        namespace = self.kwargs['namespace']
        name = self.kwargs['name']

        for item in data:
            version = item['version']
            item['href'] = reverse(
                'galaxy:api:v3:collection-version',
                kwargs=dict(namespace=namespace, name=name, version=version)
            )

    def _fix_retrieve_url(self, data):
        namespace = self.kwargs['namespace']
        name = self.kwargs['name']
        version = self.kwargs['version']

        data['href'] = reverse(
            'galaxy:api:v3:collection-version',
            kwargs=dict(namespace=namespace, name=name, version=version)
        )
        data['collection'] = reverse(
            'galaxy:api:v3:collection',
            kwargs=dict(namespace=namespace, name=name)
        )


class CollectionImportViewSet(api_base.ViewSet):

    def retrieve(self, request, pk):
        api = bindings_client.pulp_galaxy.GalaxyImportsApi(pulp.get_client())
        response = api.get(prefix=settings.X_PULP_API_PREFIX, id=pk)
        return Response(response.to_dict())


class CollectionArtifactUploadView(api_base.APIView):

    permission_classes = api_base.GALAXY_PERMISSION_CLASSES + [
        permissions.IsNamespaceOwner
    ]

    def post(self, request, *args, **kwargs):
        metrics.collection_import_attempts.inc()
        serializer = CollectionUploadSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        filename = data['filename']

        try:
            namespace = models.Namespace.objects.get(name=filename.namespace)
        except models.Namespace.DoesNotExist:
            raise ValidationError(
                'Namespace "{0}" does not exist.'.format(filename.namespace)
            )

        self.check_object_permissions(request, namespace)
        api = pulp.get_client()
        url = '{host}/{prefix}/{path}'.format(
            host=api.configuration.host,
            path='v3/artifacts/collections/',
            prefix=settings.X_PULP_API_PREFIX
        )
        headers = {}
        headers.update(api.default_headers)
        headers.update({'Content-Type': 'multipart/form-data'})

        api.update_params_for_auth(headers, tuple(), api.configuration.auth_settings())

        post_params = self._prepare_post_params(data)
        try:
            upload_response = api.request(
                'POST',
                url,
                headers=headers,
                post_params=post_params,
            )
        except bindings_client.pulp_galaxy.ApiException:
            log.exception('Failed to publish artifact %s (namespace=%s, sha256=%s) to pulp at url=%s',  # noqa
                          data['file'].name, namespace, data.get('sha256'), url)
            raise

        upload_response_data = json.loads(upload_response.data)

        task_detail = api.call_api(
            upload_response_data['task'],
            'GET',
            auth_settings=['BasicAuth'],
            response_type='CollectionImport',
            _return_http_data_only=True,
        )

        log.info('Publishing of artifact %s to namespace=%s by user=%s created pulp import task_id=%s', # noqa
                 data['file'].name, namespace, request.user, task_detail.id)

        import_obj = models.CollectionImport.objects.create(
            task_id=task_detail.id,
            created_at=task_detail.created_at,
            namespace=namespace,
            name=data['filename'].name,
            version=data['filename'].version,
        )

        metrics.collection_import_successes.inc()
        return Response(
            data={'task': import_obj.get_absolute_url()},
            status=upload_response.status
        )

    @staticmethod
    def _prepare_post_params(data):
        filename = data['filename']
        post_params = [
            ('file', (data['file'].name, data['file'].read(), data['mimetype'])),
            ('expected_namespace', filename.namespace),
            ('expected_name', filename.name),
            ('expected_version', filename.version),
        ]
        if data['sha256']:
            post_params.append(('sha256', data['sha256']))
        return post_params


class CollectionArtifactDownloadView(api_base.APIView):
    def get(self, request, *args, **kwargs):
        metrics.collection_artifact_download_attempts.inc()

        # NOTE(cutwater): Using urllib3 because it's already a dependency of pulp_galaxy
        url = 'http://{host}:{port}/{prefix}/automation-hub/{filename}'.format(
            host=settings.X_PULP_CONTENT_HOST,
            port=settings.X_PULP_CONTENT_PORT,
            prefix=settings.X_PULP_CONTENT_PATH_PREFIX.strip('/'),
            filename=self.kwargs['filename'],
        )
        response = requests.get(url, stream=True, allow_redirects=False)

        if response.status_code == requests.codes.not_found:
            metrics.collection_artifact_download_failures.labels(status=requests.codes.not_found).inc() # noqa
            raise NotFound()

        if response.status_code == requests.codes.found:
            return HttpResponseRedirect(response.headers['Location'])

        if response.status_code == requests.codes.ok:
            metrics.collection_artifact_download_successes.inc()

            return StreamingHttpResponse(
                response.iter_content(chunk_size=4096),
                content_type=response.headers['Content-Type']
            )

        metrics.collection_artifact_download_failures.labels(status=response.status_code).inc()
        raise APIException('Unexpected response from content app. '
                           f'Code: {response.status_code}.')
