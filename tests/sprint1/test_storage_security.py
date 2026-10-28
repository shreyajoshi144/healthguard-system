
import hashlib, os
from pathlib import Path
import pytest
import storage.service as storage_service
from storage.service import StorageError, sanitize_filename, validate_upload

def test_filename_is_basename_only():
    assert sanitize_filename("../../private/record.pdf") == "record.pdf"

def test_pdf_signature_and_extension_are_checked():
    data=b"%PDF-1.7 fake"
    assert validate_upload("record.pdf","application/pdf",data)=="record.pdf"
    with pytest.raises(StorageError): validate_upload("record.png","application/pdf",data)

def test_wrong_signature_rejected():
    with pytest.raises(StorageError): validate_upload("record.pdf","application/pdf",b"not-a-pdf")

def test_oversized_upload_rejected(monkeypatch):
    monkeypatch.setattr(storage_service,"MAX_FILE_SIZE",4)
    with pytest.raises(StorageError): validate_upload("record.pdf","application/pdf",b"%PDF-1.7")
