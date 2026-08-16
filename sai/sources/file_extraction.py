from io import BytesIO
from pathlib import Path

from docx import Document
from pypdf import PdfReader


class FileExtractionError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _text_file(data):
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise FileExtractionError('unsupported_encoding') from exc


def _pdf_file(data):
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise FileExtractionError('encrypted_file')

        return '\n\n'.join(page.extract_text() or '' for page in reader.pages)
    except FileExtractionError:
        raise
    except Exception as exc:
        raise FileExtractionError('file_read_failed') from exc


def _docx_file(data):
    try:
        document = Document(BytesIO(data))
        parts = [paragraph.text for paragraph in document.paragraphs]

        # 사내 규칙이 표로 정리된 문서도 빠뜨리지 않는다.
        for table in document.tables:
            for row in table.rows:
                parts.append(' | '.join(cell.text for cell in row.cells))

        return '\n'.join(parts)
    except Exception as exc:
        raise FileExtractionError('file_read_failed') from exc


def extract_file_text(file_name, data):
    extension = Path(file_name).suffix.lower()

    if extension in {'.txt', '.md'}:
        text = _text_file(data)
    elif extension == '.pdf':
        text = _pdf_file(data)
    elif extension == '.docx':
        text = _docx_file(data)
    else:
        raise FileExtractionError('file_type_not_supported')

    text = text.strip()
    if not text:
        raise FileExtractionError('empty_document')

    return text
