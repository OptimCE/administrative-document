from typing import Any

from fastapi import Request


def parse_query_params(request: Request) -> dict[str, Any]:
    """
    Parse query parameters from a FastAPI request.

    This function extracts all query parameters from a FastAPI request and
    converts them to a dictionary. It's typically used as a dependency in
    FastAPI route handlers to easily access query parameters.

    Args:
        request (Request): The FastAPI request object containing query parameters.

    Returns:
        Dict[str, Any]: A dictionary of query parameters where keys are parameter
            names and values are parameter values.
    """
    return dict(request.query_params)
