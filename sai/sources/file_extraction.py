from io import BytesIO
from pathlib import Path

from docx import Document
from pypdf import PdfReader

from config.errors import DomainError


LOCAL_DOCUMENT_PART_SIZE = 4000


class FileExtractionError(DomainError):
    pass


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


def split_file_text(text, part_size=LOCAL_DOCUMENT_PART_SIZE):
    parts = []
    current = ''

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        line_parts = [
            line[start:start + part_size]
            for start in range(0, len(line), part_size)
        ]
        for line_part in line_parts:
            candidate = f'{current}\n{line_part}'.strip() if current else line_part
            if len(candidate) <= part_size:
                current = candidate
                continue

            parts.append(current)
            current = line_part

    if current:
        parts.append(current)

    return parts
