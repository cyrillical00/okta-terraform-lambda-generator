import ast
import io
import zipfile


def validate_lambda_python(code: str) -> list[str]:
    try:
        ast.parse(code)
        return []
    except SyntaxError as e:
        return [f"Syntax error on line {e.lineno}: {e.msg}"]


def build_lambda_zip_bytes(lambda_python: str, lambda_requirements_txt: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lambda_function.py", lambda_python)
        if lambda_requirements_txt.strip():
            zf.writestr("requirements.txt", lambda_requirements_txt)
    return buffer.getvalue()
