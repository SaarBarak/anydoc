//! Python bindings for anydoc.

use std::path::PathBuf;

use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

mod document;

create_exception!(
    anydoc,
    ConvertError,
    PyException,
    "A complete conversion was impossible. Catch this to handle every kind of \
     failure, or one of the subclasses below to single one out. An unreadable \
     file raises `OSError` instead."
);

create_exception!(
    anydoc,
    UnsupportedError,
    ConvertError,
    "The format is unknown, or cannot be converted at all."
);

create_exception!(
    anydoc,
    NeedsOcrError,
    ConvertError,
    "Pages of a PDF are scanned or image-only and need OCR, which anydoc does \
     not do. `pages` lists them (1-indexed) and `page_count` is the length of \
     the document."
);

create_exception!(
    anydoc,
    MalformedError,
    ConvertError,
    "The document is structurally unusable: no meaningful content could be \
     extracted. `part` names the package part or stream at fault, and is \
     `None` when no single part is."
);

create_exception!(
    anydoc,
    EncryptedError,
    ConvertError,
    "The document is encrypted or password-protected."
);

create_exception!(
    anydoc,
    ResourceLimitError,
    ConvertError,
    "A fixed safety limit was crossed: decompression, nesting depth, node \
     count, repeat expansion, or retained asset bytes. `limit` names it."
);

create_exception!(
    anydoc,
    MissingPartError,
    ConvertError,
    "A part required for any meaningful output is absent. `part` names it."
);

/// Format names, as the extension that identifies each format. Container
/// variants that share a parser (`.docm`, `.xlsm`, `.ppsx`, ...) map onto
/// these via `format_from_bytes` or `format_from_extension`.
const FORMATS: [(&str, anydoc::Format); 12] = [
    ("doc", anydoc::Format::Doc),
    ("docx", anydoc::Format::Docx),
    ("odt", anydoc::Format::Odt),
    ("pdf", anydoc::Format::Pdf),
    ("ppt", anydoc::Format::Ppt),
    ("pptx", anydoc::Format::Pptx),
    ("rtf", anydoc::Format::Rtf),
    ("epub", anydoc::Format::Epub),
    ("xlsx", anydoc::Format::Excel),
    ("ods", anydoc::Format::Ods),
    ("odp", anydoc::Format::Odp),
    ("csv", anydoc::Format::Csv),
];

fn parse_format(name: &str) -> PyResult<anydoc::Format> {
    FORMATS.iter().find(|(n, _)| *n == name).map(|(_, format)| *format).ok_or_else(|| {
        let names: Vec<&str> = FORMATS.iter().map(|(n, _)| *n).collect();
        PyValueError::new_err(format!(
            "unknown format {name:?}; expected one of {}",
            names.join(", ")
        ))
    })
}

fn format_name(format: anydoc::Format) -> &'static str {
    FORMATS
        .iter()
        .find(|(_, f)| *f == format)
        .map(|(name, _)| *name)
        .expect("every format is named")
}

/// Raise the subclass that names the failure, carrying the part or limit at
/// fault where the variant knows one. An unreadable file raises the `OSError`
/// subclass any other read of it would.
fn convert_error(py: Python<'_>, error: anydoc::ConvertError) -> PyErr {
    let error = match error {
        anydoc::ConvertError::Io(e) => return e.into(),
        other => other,
    };
    let message = error.to_string();
    // A variant added later raises the base class until it is named here.
    let raised = match &error {
        anydoc::ConvertError::Unsupported(_) => UnsupportedError::new_err(message),
        anydoc::ConvertError::NeedsOcr { .. } => NeedsOcrError::new_err(message),
        anydoc::ConvertError::Malformed { .. } => MalformedError::new_err(message),
        anydoc::ConvertError::Encrypted => EncryptedError::new_err(message),
        anydoc::ConvertError::ResourceLimit { .. } => ResourceLimitError::new_err(message),
        anydoc::ConvertError::MissingPart { .. } => MissingPartError::new_err(message),
        _ => ConvertError::new_err(message),
    };
    let detail = match &error {
        anydoc::ConvertError::NeedsOcr { pages, page_count } => raised
            .value(py)
            .setattr("pages", pages.clone())
            .and_then(|()| raised.value(py).setattr("page_count", *page_count)),
        anydoc::ConvertError::Malformed { part, .. } => {
            raised.value(py).setattr("part", part.as_deref())
        }
        anydoc::ConvertError::ResourceLimit { limit, .. } => {
            raised.value(py).setattr("limit", *limit)
        }
        anydoc::ConvertError::MissingPart { part } => {
            raised.value(py).setattr("part", part.as_str())
        }
        _ => Ok(()),
    };
    detail.err().unwrap_or(raised)
}

/// Detect the format from the content itself: the signature and identity each
/// container specification designates (PDF header, RTF open group, OLE stream
/// names, ZIP package mimetype/content types). Plain-text formats (CSV) carry
/// no signature and return `None`; so does anything unrecognized.
#[pyfunction]
fn format_from_bytes(data: Vec<u8>) -> Option<&'static str> {
    anydoc::Format::from_bytes(&data).map(format_name)
}

/// The format an extension names, with or without a leading dot.
#[pyfunction]
fn format_from_extension(extension: &str) -> Option<&'static str> {
    anydoc::Format::from_extension(extension.trim_start_matches('.')).map(format_name)
}

/// The format a path's extension names.
#[pyfunction]
fn format_from_path(path: PathBuf) -> Option<&'static str> {
    anydoc::Format::from_path(&path).map(format_name)
}

/// Convert a document file to Markdown. The format is detected from the file
/// content; the extension is the fallback for signature-less formats (CSV)
/// and unrecognizable containers.
#[pyfunction]
fn to_markdown(py: Python<'_>, path: PathBuf) -> PyResult<String> {
    py.detach(|| anydoc::to_markdown(&path)).map_err(|e| convert_error(py, e))
}

/// Convert an in-memory document to Markdown. Without a format, it is
/// detected from the content, which signature-less formats (CSV) have to name
/// explicitly.
#[pyfunction]
#[pyo3(signature = (data, format=None))]
fn to_markdown_bytes(py: Python<'_>, data: Vec<u8>, format: Option<&str>) -> PyResult<String> {
    let format = format.map(parse_format).transpose()?;
    py.detach(|| anydoc::to_markdown_bytes(&data, format)).map_err(|e| convert_error(py, e))
}

/// Parse an in-memory document into the document model, which also carries
/// the embedded assets. Without a format, it is detected from the content.
///
/// Unsupported for `pdf`: PDF conversion produces Markdown directly and has
/// no document-model form; use `to_markdown_bytes`.
#[pyfunction]
#[pyo3(signature = (data, format=None))]
fn to_document(
    py: Python<'_>,
    data: Vec<u8>,
    format: Option<&str>,
) -> PyResult<document::Document> {
    let format = format.map(parse_format).transpose()?;
    let parsed =
        py.detach(|| anydoc::to_document(&data, format)).map_err(|e| convert_error(py, e))?;
    document::document(py, parsed)
}

/// Per-page extraction state for a PDF: one dict per page carrying `page`
/// (0-indexed), `markdown`, `needs_ocr`, and `ocr_reason`.
///
/// This is the same read anydoc's own PDF backend performs, exposed so a
/// caller can see which pages the extractor distrusts without installing
/// `pdf-inspector` as a second Python package. That second package resolves
/// independently of the crate this binary links, which is how the two ended
/// up on different versions — the crate carrying the RTL fix, the Python
/// package not — and Hebrew came back character-reversed from the Python
/// side. Reading it here means there is one version, and no way for them to
/// disagree.
///
/// A page whose `needs_ocr` is true returns an empty `markdown`: the
/// extractor suppresses text it does not trust. `pdf_text_positions` still
/// reports that page's items, which is what makes the suppression
/// recoverable.
#[pyfunction]
fn pdf_pages_markdown<'py>(py: Python<'py>, data: Vec<u8>) -> PyResult<Bound<'py, PyList>> {
    let extraction = py
        .detach(|| pdf_inspector::extract_pages_markdown_mem(&data, None))
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let pages = PyList::empty(py);
    for page in extraction.pages {
        let entry = PyDict::new(py);
        entry.set_item("page", page.page)?;
        entry.set_item("markdown", page.markdown)?;
        entry.set_item("needs_ocr", page.needs_ocr)?;
        // `None` when the extractor distrusted the page without being able to
        // say why. The dispatch's dropped-page guard keys on this: a page that
        // is empty *and* states a reason lost text, where a genuinely blank
        // page reports `needs_ocr` with no reason at all.
        entry.set_item("ocr_reason", page.ocr_reason)?;
        pages.append(entry)?;
    }
    Ok(pages)
}

/// Every positioned text item in a PDF: one dict per item carrying `text`,
/// `page` (1-indexed), `x`, `y`, `width`, `height`, and `is_image`.
///
/// Reports what the page draws, with no judgement about whether to trust it,
/// so it still returns items for pages `pdf_pages_markdown` suppresses.
/// `is_image` marks placeholders standing in for embedded images rather than
/// real text — counting those as characters makes an empty page look
/// populated.
#[pyfunction]
fn pdf_text_positions<'py>(py: Python<'py>, data: Vec<u8>) -> PyResult<Bound<'py, PyList>> {
    let items = py
        .detach(|| pdf_inspector::extractor::extract_text_with_positions_mem(&data))
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let out = PyList::empty(py);
    for item in items {
        let entry = PyDict::new(py);
        entry.set_item("text", item.text)?;
        entry.set_item("page", item.page)?;
        entry.set_item("x", item.x)?;
        entry.set_item("y", item.y)?;
        entry.set_item("width", item.width)?;
        entry.set_item("height", item.height)?;
        entry.set_item(
            "is_image",
            matches!(item.item_type, pdf_inspector::types::ItemType::Image),
        )?;
        out.append(entry)?;
    }
    Ok(out)
}

/// Convert documents to GitHub-Flavored Markdown.
#[pymodule]
fn _anydoc(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(format_from_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(format_from_extension, m)?)?;
    m.add_function(wrap_pyfunction!(format_from_path, m)?)?;
    m.add_function(wrap_pyfunction!(to_markdown, m)?)?;
    m.add_function(wrap_pyfunction!(to_markdown_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(to_document, m)?)?;
    m.add_function(wrap_pyfunction!(pdf_pages_markdown, m)?)?;
    m.add_function(wrap_pyfunction!(pdf_text_positions, m)?)?;
    m.add_class::<document::Asset>()?;
    m.add_class::<document::Block>()?;
    m.add_class::<document::Cell>()?;
    m.add_class::<document::CellSlot>()?;
    m.add_class::<document::Document>()?;
    m.add_class::<document::ImageSource>()?;
    m.add_class::<document::Inline>()?;
    m.add_class::<document::LinkTarget>()?;
    m.add_class::<document::List>()?;
    m.add_class::<document::ListItem>()?;
    m.add_class::<document::Note>()?;
    m.add_class::<document::Style>()?;
    m.add_class::<document::Table>()?;
    m.add("ConvertError", m.py().get_type::<ConvertError>())?;
    m.add("EncryptedError", m.py().get_type::<EncryptedError>())?;
    m.add("MalformedError", m.py().get_type::<MalformedError>())?;
    m.add("MissingPartError", m.py().get_type::<MissingPartError>())?;
    m.add("NeedsOcrError", m.py().get_type::<NeedsOcrError>())?;
    m.add("ResourceLimitError", m.py().get_type::<ResourceLimitError>())?;
    m.add("UnsupportedError", m.py().get_type::<UnsupportedError>())?;
    Ok(())
}
