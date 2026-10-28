from __future__ import annotations
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import os
from typing import BinaryIO

@dataclass(frozen=True)
class ExtractedBlock:
    page_number: int
    source_text: str
    extraction_method: str
    confidence: float | None = None
    block_index: int = 0

class ExtractionError(RuntimeError):
    pass

class DocumentExtractor:
    supported_types = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
    max_pages = int(os.getenv("HEALTHGUARD_MAX_DOCUMENT_PAGES", "100"))
    max_extracted_chars = int(os.getenv("HEALTHGUARD_MAX_EXTRACTED_TEXT_CHARS", "200000"))
    max_image_pixels = int(os.getenv("HEALTHGUARD_MAX_IMAGE_PIXELS", "25000000"))

    def extract(self, stream: BinaryIO, content_type: str) -> list[ExtractedBlock]:
        if content_type == "application/pdf":
            return self._extract_pdf(stream)
        if content_type in {"image/jpeg", "image/png", "image/webp"}:
            return [self._ocr_image(stream, 1)]
        raise ExtractionError(f"Unsupported processing content type: {content_type}")

    def _extract_pdf(self, stream: BinaryIO) -> list[ExtractedBlock]:
        try:
            import pdfplumber
        except ImportError as exc:
            raise ExtractionError("PDF extraction dependency is unavailable.") from exc
        blocks: list[ExtractedBlock] = []
        try:
            stream.seek(0)
            with pdfplumber.open(stream) as pdf:
                if len(pdf.pages) > self.max_pages:
                    raise ExtractionError("Document contains too many pages.")
                total_chars = 0
                for page_number, page in enumerate(pdf.pages, start=1):
                    text = (page.extract_text() or "").strip()
                    total_chars += len(text)
                    if total_chars > self.max_extracted_chars:
                        raise ExtractionError("Document contains too much extracted text.")
                    if text:
                        blocks.append(ExtractedBlock(page_number, text, "PDF_TEXT", None, 0))
                    else:
                        image = page.to_image(resolution=150, antialias=True).original
                        blocks.append(self._ocr_pil(image, page_number))
        except Exception as exc:
            raise ExtractionError("PDF extraction failed.") from exc
        if not blocks:
            raise ExtractionError("PDF contains no pages.")
        return blocks

    def _ocr_image(self, stream: BinaryIO, page_number: int) -> ExtractedBlock:
        try:
            from PIL import Image
            stream.seek(0)
            image = Image.open(stream)
            if image.width * image.height > self.max_image_pixels:
                raise ExtractionError("Image is too large to process safely.")
            image = image.convert("RGB")
            return self._ocr_pil(image, page_number)
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError("Image OCR failed.") from exc

    def _ocr_pil(self, image, page_number: int) -> ExtractedBlock:
        if image.width * image.height > self.max_image_pixels:
            raise ExtractionError("Image is too large to process safely.")
        try:
            import pytesseract
            from pytesseract import Output
            data = pytesseract.image_to_data(image, output_type=Output.DICT)
        except Exception as exc:
            raise ExtractionError("OCR engine is unavailable or failed.") from exc
        parts = []
        confidences = []
        for text, conf in zip(data.get("text", []), data.get("conf", [])):
            value = (text or "").strip()
            if not value:
                continue
            parts.append(value)
            try:
                numeric = float(conf)
                if numeric >= 0:
                    confidences.append(numeric / 100.0)
            except (TypeError, ValueError):
                pass
        source_text = " ".join(parts).strip()
        if len(source_text) > self.max_extracted_chars:
            raise ExtractionError("OCR produced too much extracted text.")
        if not source_text:
            raise ExtractionError(f"OCR produced no text for page {page_number}.")
        confidence = sum(confidences) / len(confidences) if confidences else None
        return ExtractedBlock(page_number, source_text, "OCR", confidence, 0)

extractor = DocumentExtractor()


def check_ocr_runtime() -> str:
    """Verify the Tesseract binary and its English data are usable; returns the version string.

    Used by the worker at startup so a runtime without Tesseract (e.g. a native Render Python service) fails loudly
    instead of failing every scanned document later with "OCR engine is unavailable".
    """
    try:
        import pytesseract
        version = str(pytesseract.get_tesseract_version())
        languages = pytesseract.get_languages(config="")
    except Exception as exc:
        raise ExtractionError("Tesseract OCR runtime is not installed or cannot be executed.") from exc
    if "eng" not in languages:
        raise ExtractionError("Tesseract English language data ('eng') is not installed.")
    return version
