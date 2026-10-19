"""Compatibility entry point for the shared local parser."""

from api.services.documents.file_parser import main, parse_file  # noqa: F401

if __name__ == "__main__":
    main()
