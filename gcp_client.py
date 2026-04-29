import json

from google.api_core.exceptions import GoogleAPICallError, NotFound, PermissionDenied
from google.auth.exceptions import DefaultCredentialsError
from google.oauth2 import service_account


class GcpError(Exception):
    pass


def _build_credentials(sa_json: str):
    if not sa_json:
        return None
    # Strip UTF-8 BOM if present — pastes from Notepad / PowerShell secrets often have one
    sa_json = sa_json.lstrip("﻿").strip()
    try:
        info = json.loads(sa_json)
    except json.JSONDecodeError as e:
        raise GcpError(f"GCP_SA_JSON is not valid JSON: {e}") from e
    try:
        # cloud-platform scope is required for SA credentials to mint usable
        # access tokens for the Cloud Functions / IAM / Pub/Sub / Run APIs we
        # call. Without it, the token request returns a credential that 401s.
        return service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    except KeyError as e:
        raise GcpError(f"GCP_SA_JSON missing required service-account fields: {e}") from e
    except ValueError as e:
        raise GcpError(f"GCP_SA_JSON could not be parsed (invalid private key or shape): {e}") from e


class GcpClient:
    def __init__(self, project_id: str, sa_json: str = "", region: str = "us-central1"):
        if not project_id:
            raise GcpError("GCP project_id is required.")
        self.project = project_id
        self.region = region
        self._creds = _build_credentials(sa_json)

        try:
            from google.cloud import functions_v2
            from google.cloud import iam_admin_v1
            from google.cloud import pubsub_v1
            from google.cloud import run_v2
        except ImportError as e:
            raise GcpError(
                "Google Cloud SDK packages not installed. "
                "Run: pip install google-cloud-functions google-cloud-iam "
                "google-cloud-pubsub google-cloud-run"
            ) from e

        try:
            self._functions = functions_v2.FunctionServiceClient(credentials=self._creds)
            self._iam = iam_admin_v1.IAMClient(credentials=self._creds)
            self._pubsub = pubsub_v1.PublisherClient(credentials=self._creds)
            self._run = run_v2.ServicesClient(credentials=self._creds)
        except DefaultCredentialsError as e:
            raise GcpError(
                "No GCP credentials found. Add GCP_SA_JSON to secrets or run "
                "`gcloud auth application-default login`."
            ) from e
        except Exception as e:
            raise GcpError(f"Failed to initialise GCP clients: {e}") from e

    def _handle(self, exc: Exception) -> None:
        if isinstance(exc, PermissionDenied):
            raise GcpError(
                f"GCP permission denied: {exc}. The service account needs viewer roles on "
                "Cloud Functions, IAM, Pub/Sub, and Cloud Run."
            ) from exc
        raise GcpError(str(exc)) from exc

    def list_functions(self) -> list[dict]:
        parent = f"projects/{self.project}/locations/{self.region}"
        try:
            result = []
            for fn in self._functions.list_functions(parent=parent):
                result.append({
                    "name": fn.name.split("/")[-1],
                    "full_name": fn.name,
                    "uri": fn.service_config.uri if fn.service_config else "",
                    "runtime": fn.build_config.runtime if fn.build_config else "",
                })
            return result
        except (GoogleAPICallError, PermissionDenied) as e:
            self._handle(e)

    def list_service_accounts(self) -> list[dict]:
        try:
            from google.cloud.iam_admin_v1 import types
        except ImportError as e:
            raise GcpError(f"google-cloud-iam types unavailable: {e}") from e
        request = types.ListServiceAccountsRequest(name=f"projects/{self.project}")
        try:
            result = []
            for sa in self._iam.list_service_accounts(request=request).accounts:
                result.append({
                    "email": sa.email,
                    "display_name": sa.display_name,
                    "unique_id": sa.unique_id,
                })
            return result
        except (GoogleAPICallError, PermissionDenied) as e:
            self._handle(e)

    def list_pubsub_topics(self) -> list[dict]:
        project_path = f"projects/{self.project}"
        try:
            result = []
            for topic in self._pubsub.list_topics(request={"project": project_path}):
                result.append({
                    "name": topic.name.split("/")[-1],
                    "full_name": topic.name,
                })
            return result
        except (GoogleAPICallError, PermissionDenied) as e:
            self._handle(e)

    def list_run_services(self) -> list[dict]:
        parent = f"projects/{self.project}/locations/{self.region}"
        try:
            result = []
            for svc in self._run.list_services(parent=parent):
                result.append({
                    "name": svc.name.split("/")[-1],
                    "full_name": svc.name,
                    "uri": svc.uri,
                })
            return result
        except (GoogleAPICallError, PermissionDenied) as e:
            self._handle(e)

    def get_function_by_name(self, name: str) -> dict | None:
        full = f"projects/{self.project}/locations/{self.region}/functions/{name}"
        try:
            fn = self._functions.get_function(name=full)
            return {
                "name": fn.name.split("/")[-1],
                "full_name": fn.name,
                "uri": fn.service_config.uri if fn.service_config else "",
            }
        except NotFound:
            return None
        except (GoogleAPICallError, PermissionDenied) as e:
            self._handle(e)

    def get_service_account_by_email(self, email: str) -> dict | None:
        try:
            from google.cloud.iam_admin_v1 import types
        except ImportError as e:
            raise GcpError(f"google-cloud-iam types unavailable: {e}") from e
        request = types.GetServiceAccountRequest(
            name=f"projects/{self.project}/serviceAccounts/{email}"
        )
        try:
            sa = self._iam.get_service_account(request=request)
            return {
                "email": sa.email,
                "display_name": sa.display_name,
                "unique_id": sa.unique_id,
            }
        except NotFound:
            return None
        except (GoogleAPICallError, PermissionDenied) as e:
            self._handle(e)
