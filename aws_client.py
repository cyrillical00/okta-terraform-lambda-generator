import boto3
from botocore.exceptions import ClientError, NoCredentialsError, EndpointResolutionError


class AWSError(Exception):
    pass


class AWSClient:
    def __init__(self, region: str, access_key: str = "", secret_key: str = ""):
        kwargs = {"region_name": region}
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
        try:
            self._lambda = boto3.client("lambda", **kwargs)
            self._iam = boto3.client("iam", **kwargs)
        except Exception as e:
            raise AWSError(f"Failed to initialise AWS clients: {e}") from e

    def _handle(self, exc: Exception) -> None:
        if isinstance(exc, NoCredentialsError):
            raise AWSError("No AWS credentials found. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in secrets.") from exc
        raise AWSError(str(exc)) from exc

    def list_lambda_functions(self) -> list[dict]:
        try:
            paginator = self._lambda.get_paginator("list_functions")
            result = []
            for page in paginator.paginate():
                for fn in page["Functions"]:
                    result.append({
                        "name": fn["FunctionName"],
                        "arn": fn["FunctionArn"],
                        "runtime": fn.get("Runtime", ""),
                        "description": fn.get("Description", ""),
                    })
            return result
        except (ClientError, NoCredentialsError) as e:
            self._handle(e)

    def list_iam_roles(self, path_prefix: str = "/") -> list[dict]:
        try:
            paginator = self._iam.get_paginator("list_roles")
            result = []
            for page in paginator.paginate(PathPrefix=path_prefix):
                for role in page["Roles"]:
                    result.append({
                        "name": role["RoleName"],
                        "arn": role["Arn"],
                    })
            return result
        except (ClientError, NoCredentialsError) as e:
            self._handle(e)

    def get_lambda_by_name(self, name: str) -> dict | None:
        try:
            resp = self._lambda.get_function(FunctionName=name)
            fn = resp["Configuration"]
            return {
                "name": fn["FunctionName"],
                "arn": fn["FunctionArn"],
                "runtime": fn.get("Runtime", ""),
            }
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                return None
            self._handle(e)
        except NoCredentialsError as e:
            self._handle(e)

    def get_lambda_by_arn(self, arn: str) -> dict | None:
        name = arn.split(":")[-1]
        return self.get_lambda_by_name(name)

    def get_role_by_name(self, name: str) -> dict | None:
        try:
            resp = self._iam.get_role(RoleName=name)
            role = resp["Role"]
            return {"name": role["RoleName"], "arn": role["Arn"]}
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchEntityException":
                return None
            self._handle(e)
        except NoCredentialsError as e:
            self._handle(e)
