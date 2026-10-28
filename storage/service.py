from __future__ import annotations
import os, re, uuid, io, zipfile, shutil, hashlib
from pathlib import Path
from typing import BinaryIO
from domain.observability import inc, observe
try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:
    boto3=None
    BotoConfig=None

MAX_FILE_SIZE=int(os.getenv("HEALTHGUARD_MAX_DOCUMENT_BYTES",str(20*1024*1024)))
ALLOWED={
    "application/pdf":(".pdf",b"%PDF"),
    "image/jpeg":(".jpg",b"\xff\xd8\xff"),
    "image/png":(".png",b"\x89PNG\r\n\x1a\n"),
    "image/webp":(".webp",b"RIFF"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":(".docx",b"PK"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":(".xlsx",b"PK"),
}
class StorageError(Exception): pass

def sanitize_filename(name:str)->str:
    name=Path(name or "document").name
    name=re.sub(r"[^A-Za-z0-9._-]+","_",name)[:180]
    return name or "document"

def _validate_common(filename:str,content_type:str,size:int,header:bytes,stream:BinaryIO|None=None)->str:
    if size<=0: raise StorageError("Uploaded file is empty.")
    if size>MAX_FILE_SIZE: raise StorageError("File exceeds the allowed size.")
    if content_type not in ALLOWED: raise StorageError("Unsupported document type.")
    expected_ext,magic=ALLOWED[content_type]
    safe=sanitize_filename(filename)
    if Path(safe).suffix.lower()!=expected_ext and not (expected_ext==".jpg" and Path(safe).suffix.lower()==".jpeg"):
        raise StorageError("File extension does not match its content type.")
    if not header.startswith(magic): raise StorageError("File signature does not match its declared type.")
    if content_type=="image/webp" and len(header)>=12 and header[8:12]!=b"WEBP": raise StorageError("File signature does not match its declared type.")
    if content_type.endswith("wordprocessingml.document") or content_type.endswith("spreadsheetml.sheet"):
        if stream is None: raise StorageError("Office document validation requires a file stream.")
        try:
            stream.seek(0)
            with zipfile.ZipFile(stream) as z:
                names=set(z.namelist())
                if "[Content_Types].xml" not in names or not any(n.startswith("word/") or n.startswith("xl/") for n in names):
                    raise StorageError("Invalid Office document container.")
                if any(name.startswith("/") or ".." in Path(name).parts for name in names):
                    raise StorageError("Unsafe archive paths are not allowed.")
        except zipfile.BadZipFile:
            raise StorageError("Invalid Office document container.")
        finally:
            stream.seek(0)
    return safe

def validate_upload(filename:str,content_type:str,data:bytes):
    return _validate_common(filename,content_type,len(data),data[:64],io.BytesIO(data))

def validate_upload_file(path:Path,filename:str,content_type:str,size:int)->str:
    with path.open("rb") as f:
        header=f.read(64)
        return _validate_common(filename,content_type,size,header,f)

def stream_to_temp(upload:BinaryIO, *, max_size:int=MAX_FILE_SIZE, chunk_size:int=1024*1024):
    import tempfile
    fd,temp=tempfile.mkstemp(prefix="healthguard-upload-")
    os.close(fd)
    path=Path(temp)
    size=0
    digest=hashlib.sha256()
    try:
        with path.open("wb") as out:
            while True:
                chunk=upload.read(chunk_size)
                if not chunk: break
                size+=len(chunk)
                if size>max_size: raise StorageError("File exceeds the allowed size.")
                out.write(chunk); digest.update(chunk)
        return path,size,digest.hexdigest()
    except Exception:
        path.unlink(missing_ok=True)
        raise

class Storage:
    def __init__(self):
        self.backend=os.getenv("STORAGE_BACKEND","local").lower()
        self.root=Path(os.getenv("STORAGE_LOCAL_ROOT","./private_storage")).resolve()
        if os.getenv("APP_ENV","development").lower()=="production" and self.backend!="s3": raise StorageError("Production deployments must use private S3-compatible object storage.")
        self.bucket=os.getenv("STORAGE_BUCKET")
        self.endpoint=os.getenv("STORAGE_ENDPOINT")
        if self.backend=="s3" and not self.bucket: raise StorageError("STORAGE_BUCKET is required for S3 storage.")
    def key(self,patient_id,document_id,filename):
        return f"patients/{patient_id}/documents/{document_id}/{uuid.UUID(str(document_id))}-{sanitize_filename(filename)}"
    def _s3(self):
        if boto3 is None or BotoConfig is None: raise StorageError("S3 storage requires boto3.")
        connect_timeout=float(os.getenv("HEALTHGUARD_S3_CONNECT_TIMEOUT_SECONDS","5"))
        read_timeout=float(os.getenv("HEALTHGUARD_S3_READ_TIMEOUT_SECONDS","30"))
        max_attempts=int(os.getenv("HEALTHGUARD_S3_MAX_ATTEMPTS","3"))
        if connect_timeout <= 0 or read_timeout <= 0 or max_attempts < 1 or max_attempts > 5:
            raise StorageError("Invalid S3 timeout/retry configuration.")
        config=BotoConfig(connect_timeout=connect_timeout,read_timeout=read_timeout,retries={"max_attempts":max_attempts,"mode":"standard"})
        return boto3.client("s3",endpoint_url=self.endpoint or None,aws_access_key_id=os.getenv("STORAGE_ACCESS_KEY"),aws_secret_access_key=os.getenv("STORAGE_SECRET_KEY"),region_name=os.getenv("STORAGE_REGION"),config=config)
    def put_file(self,key,path:Path,content_type:str):
        started=__import__('time').monotonic()
        try:
            if self.backend=="s3":
                client=self._s3()
                extra={"ContentType":content_type,"ServerSideEncryption":os.getenv("STORAGE_SSE","AES256")}
                client.upload_file(str(path),self.bucket,key,ExtraArgs=extra)
            else:
                target=(self.root/key).resolve()
                if self.root not in target.parents: raise StorageError("Invalid storage path.")
                target.parent.mkdir(parents=True,exist_ok=True)
                tmp=target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                try:
                    with path.open("rb") as src:
                        with tmp.open("wb") as target_file:
                            shutil.copyfileobj(src,target_file,length=1024*1024)
                    os.replace(tmp,target)
                finally:
                    tmp.unlink(missing_ok=True)
            inc("healthguard_storage_operations_total", operation="put", outcome="success")
            return key
        except Exception as exc:
            inc("healthguard_storage_operations_total", operation="put", outcome="failure", error_type=type(exc).__name__)
            raise
        finally:
            observe("healthguard_storage_operation_duration", (__import__('time').monotonic()-started)*1000, operation="put")

    def put(self,key,data,content_type):
        # Kept for compatibility with existing callers/tests; production upload routes use put_file().
        if len(data)>MAX_FILE_SIZE: raise StorageError("File exceeds the allowed size.")
        import tempfile
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(data); tmp.flush()
            return self.put_file(key,Path(tmp.name),content_type)
    def get(self,key):
        started=__import__('time').monotonic()
        try:
            if self.backend=="s3":
                result=self._s3().get_object(Bucket=self.bucket,Key=key)["Body"].read()
            else:
                target=(self.root/key).resolve()
                if self.root not in target.parents or not target.is_file(): raise StorageError("Document not found.")
                result=target.read_bytes()
            inc("healthguard_storage_operations_total", operation="get", outcome="success")
            return result
        except Exception as exc:
            inc("healthguard_storage_operations_total", operation="get", outcome="failure", error_type=type(exc).__name__)
            raise
        finally:
            observe("healthguard_storage_operation_duration", (__import__('time').monotonic()-started)*1000, operation="get")

    def delete(self,key):
        started=__import__('time').monotonic()
        try:
            if self.backend=="s3":
                self._s3().delete_object(Bucket=self.bucket,Key=key)
            else:
                target=(self.root/key).resolve()
                if self.root not in target.parents: raise StorageError("Invalid storage path.")
                if target.exists(): target.unlink()
            inc("healthguard_storage_operations_total", operation="delete", outcome="success")
        except Exception as exc:
            inc("healthguard_storage_operations_total", operation="delete", outcome="failure", error_type=type(exc).__name__)
            raise
        finally:
            observe("healthguard_storage_operation_duration", (__import__('time').monotonic()-started)*1000, operation="delete")

    def health_check(self):
        if self.backend != "s3":
            return self.root.exists()
        client=self._s3()
        started=__import__('time').monotonic()
        try:
            client.head_bucket(Bucket=self.bucket)
            inc("healthguard_dependency_health_checks_total", dependency="storage", outcome="success")
            return True
        except Exception as exc:
            inc("healthguard_dependency_failures_total", dependency="storage", error_type=type(exc).__name__)
            raise
        finally:
            observe("healthguard_storage_health_duration", (__import__('time').monotonic()-started)*1000, dependency="storage")

    def signed_url(self,key,expires=300):
        if self.backend!="s3": return None
        return self._s3().generate_presigned_url("get_object",Params={"Bucket":self.bucket,"Key":key},ExpiresIn=expires)

storage=Storage()
