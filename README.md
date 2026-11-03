# PDF to Kindle

![PDF to Kindle app icon](assets/app-icon.png)

A local macOS app that turns PDF files into reflowable EPUB books for **Send to Kindle**.

The window resizes itself to fit the settings that are visible. The AI settings sit in compact horizontal rows, so the action buttons and the status area are usually visible without scrolling, even with AI turned on. On very small screens, scrolling still works as a fallback.

The app follows macOS light and dark appearance automatically. Text, fields, status cards, dropdown menus and disabled controls switch to a high-contrast palette, also when the system appearance changes while the app is open.

During a conversion, every setting and action button except **Cancel** is disabled. A highlighted phase label tells apart local processing, waiting for the user's AI approval, processing through the OpenAI API, and building the Kindle file locally. The detailed status shows the current page or text chunk number.

## Getting started

1. Double-click **PDF to Kindle.app** in the project folder.
2. Choose a PDF, title, author and OCR language.
3. Click **Create Kindle file…** and upload the finished `.epub` with Send to Kindle.

During the conversion the app shows a running estimate of the time left. **Cancel** stops the active PDF rendering or OCR process and cleans up temporary files. The settings are locked while the conversion runs.

The app automatically removes page numbers and headers and footers that repeat across the document.

## Digital PDFs and scans

Many PDFs already contain real text. When a page has a usable text layer, the app reads that text directly instead of running OCR, which is both faster and free of OCR errors. Pages that are scanned images, or whose text layer is missing or garbled, are OCR'd as before, so a mixed PDF works too. The status line shows how many pages needed OCR. The behaviour can be turned off with **Use the PDF's own text when it has one**, for example if a PDF's built-in text is from a poor earlier OCR run.

On pages with a text layer the app also:

- turns tables into real EPUB tables instead of loose lines of text,
- reads two-column pages column by column instead of across both columns,
- splits paragraphs that the PDF stores as one text block.

Small numbered notes at the bottom of a page are marked as footnotes and shown in a smaller font, on scanned pages as well.

OCR runs on several pages at once (up to four, depending on the Mac's CPU cores), and each page is rendered and handed to OCR as soon as it is ready, so big scanned books finish noticeably faster.

From Terminal:

```bash
python3 app.py
```

If macOS blocks the app the first time, right-click it and choose **Open**.
`start.command` can be used as a fallback.

The app bundle contains its own copy of the program code, so macOS does not have to grant the launcher separate access to files outside the app. After changing the source code, update the bundle by running `./build_app.command`.

## Requirements

- Python 3 with Tkinter and Pillow
- PyMuPDF (`pip install pymupdf`) or Poppler (`pdftoppm`)
- Tesseract OCR

The app offers Danish, English, German, French, Spanish, Norwegian and Swedish. When the user picks a language that is not installed, the official `tessdata_fast` model is downloaded right away. The status line and progress bar show the download. The model is stored in the app and reused next time.

The app does not store or upload documents. OCR runs locally. Blocks, headings, lists and indentation are rebuilt from the OCR coordinates. Photos, diagrams and other dense areas without text are cropped out and placed in reading order between the text blocks.

## Chapters

Kindle chapters are created from clear chapter markers and OCR typography. Markers such as **Chapter 3**, **Part II** and numbered titles are recognized directly (Danish markers such as **Kapitel 3** and **Del II** work too). A short typographic heading can also start a chapter when it comes first on a page after a reasonable amount of body text. Ordinary PDF page breaks do not create chapters.

## Optional AI post-processing

**AI post-processing** sends the OCR text in small chunks to the OpenAI Responses API. The model menu is loaded dynamically from the models the API project has access to. When OCR is done, the app shows estimated input tokens, output tokens and total token usage, and asks for approval before the text is sent. The model is told to fix only clear OCR errors, hyphenation, spacing and punctuation, without shortening, translating or rewriting the text. The feature is off by default and needs an OpenAI API key. The key can be stored securely in the macOS Keychain. API usage is billed separately by OpenAI and is not necessarily included in a ChatGPT subscription.

The system instructions sent to OpenAI are written in English so they are unambiguous across models. They also explicitly require that the document's original language is kept and that the text is never translated or rewritten.

**AI for illustrations** can be chosen separately. A local filter first picks pages with likely illustrations, and only those page images are sent to the chosen multimodal model. The model tells illustrations apart from text, returns crop coordinates and picks the nearest OCR text block as the placement. The crop itself is done locally on the high-resolution page. The dialog before the API call shows both the number of page images and a conservative token estimate.

The AI panel is hidden until **Use the OpenAI API to improve the result** is checked. There, text correction and image analysis can be turned on independently, the model can be chosen, and the workflow can be set to either **Ask before sending** or **Send automatically after OCR**.

**Refresh models** first loads the API project's current model access. A small background call then picks three relevant models and searches only official OpenAI documentation for descriptions and standard prices. No model names, descriptions or prices are hardcoded in the app. A price is shown only when the metadata call can confirm it; otherwise the app shows **Price unknown**. The result is cached locally and reused between launches. The app only queries models again when the user clicks **Refresh models**.

The API key can be stored in the macOS Keychain and loaded automatically at the next launch. It is never written to project files or log files and can be removed again with the ✕ button. HTTPS calls use macOS' verified CA bundle (with `certifi` as a fallback); SSL verification is never turned off.

## Fault tolerance

- Temporary OpenAI errors, timeouts and rate limits are retried automatically up to six times. After the first quick retries the app uses longer, visible pauses, so a short API or network hiccup late in a big book does not push the rest over to local OCR.
- AI text is split into short chunks of about 3,500 characters. During each API call the app shows the elapsed waiting time and the current attempt; the local ETA is hidden. After the first finished AI call, the remaining AI time is estimated from measured response times.
- Network calls run in a separate cancellable thread, so **Cancel** reacts at once instead of waiting for an HTTP timeout.
- The waiting status always names the overall unit, for example **Text chunk 3 of 12** or **Image page 4 of 20**, so a new API call does not look like a reset loop. Each attempt has a hard limit of 180 seconds. Between attempts the status counts down through pauses of normally 2, 8, 30, 90 and 180 seconds; a longer `Retry-After` from the API is respected.
- All transport retries for the same text chunk use the same idempotency key. The app can therefore retry safely if the connection drops after sending, without deliberately creating a call that was already received as a new job. Client and server request IDs are logged without document text or API key, so persistent errors can be traced.
- A used-up API quota, an invalid key, an invalid model reply or a persistent network error does not throw away the OCR work. The app finishes a local EPUB automatically and says clearly which AI part was skipped.
- AI text corrections are kept chunk by chunk. If a later chunk fails, corrections from all finished chunks are kept, and only the affected chunk falls back to the OCR text.
- AI replies for text and illustrations must be strict structured JSON following a schema. The parser can also extract JSON if a model adds explanatory text around the reply.
- If AI image analysis fails, the partial AI results are dropped and the local illustration analysis is used as a fallback.
- The EPUB is written and validated under a temporary name and only replaces the target file once the whole file is valid.
- Trying to quit the app during processing asks for confirmation and cancels in a controlled way.

The small **Developer** link in the top right corner opens a rotating log file under `~/Library/Logs/PDF to Kindle/`. **Open log** shows the file, and **Copy diagnostics** collects the system version, Python version, chosen settings and the most recent log lines. The log rotates at 2 MB and keeps at most three older files. API keys, document text and page images are never logged.

During a conversion the app runs macOS `caffeinate`, which prevents idle sleep until the process is done or cancelled. The screen can still turn off. Forced sleep, for example when the lid is closed, is handled after wake-up by API timeouts, retries and local fallback; the EPUB is still only replaced atomically after validation.

## Standalone macOS app

Run `./build_standalone.command` to build `standalone-dist/PDF to Kindle.app`. This version contains Python, Pillow, PyMuPDF, Tesseract and English OCR data and can be moved out of the project folder or into Applications. Poppler is not needed in the standalone version, because PDF pages are rendered with the bundled PyMuPDF runtime.

Other supported OCR languages are still downloaded only when chosen. In the standalone version they are stored in `~/Library/Application Support/PDF to Kindle/tessdata`, so the app bundle does not need to be writable.

The build is ad hoc signed locally and does not need an Apple Developer certificate. It targets the Mac architecture it is built on. The first launch may require right-click → **Open**, because the app is not notarized by Apple.

Settings saved by earlier versions under the Danish name `PDF til Kindle` are moved to the new folders automatically at the first launch.

## Tests

```bash
python3 -m unittest test_image_pipeline test_text_layer
```

## Known limitations

- Tables and multi-column text on scanned pages may still need manual cleanup. Footnotes stay at the bottom of their page instead of becoming linked notes.
- Very decorative pages and illustrations with text on top of the image may still need manual cleanup.
- Handwriting and very poor scans depend on Tesseract's OCR quality.
