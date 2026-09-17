"""OCR engine clients: one module per engine (`azure_di.py`, and in future
`tesseract.py` etc.), each implementing `base.OcrClient`. Deliberately not
re-exported here -- callers import the specific engine module they need
(e.g. `from anydoc.ocr_clients.azure_di import AzureDiClient`), keeping this
package importable with zero third-party dependencies; only importing a
specific engine's module, and calling its methods, requires that engine's
own optional dependencies."""
