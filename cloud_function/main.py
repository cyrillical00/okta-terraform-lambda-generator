"""Cloud Functions Gen2 entry point template.

Generated handler code is written here when output_mode is "GCP only" or
"Okta + GCP". Build and deploy via the generated terraform_gcp_hcl, which
expects a zip at ../cloud_function/cloud_function.zip.

Bundle command:
    cd cloud_function && zip cloud_function.zip main.py requirements.txt
"""

import functions_framework


@functions_framework.http
def main(request):
    return {"status": "ok"}, 200, {"Content-Type": "application/json"}
