# BuildingLink Package Audit Tool

A desktop application built to streamline package audits for residential concierge and property management operations.

The application parses BuildingLink Event Log PDF exports and transforms them into an interactive auditing workflow. Instead of manually marking printed audit sheets and writing audit summaries by hand, auditors can verify packages, record exceptions, and generate standardized audit reports automatically.

---

## Problem

Package audits were previously performed using printed BuildingLink reports.

A typical workflow involved:

* Printing BuildingLink package reports
* Manually marking verified packages
* Writing package discrepancies by hand
* Tracking double logged packages separately
* Creating audit summaries manually

This process was repetitive, time consuming, and prone to transcription errors.

---

## Solution

This application automates the audit workflow by:

1. Parsing BuildingLink Event Log PDF exports
2. Creating an interactive audit interface
3. Tracking package verification status
4. Recording package discrepancies
5. Recording double logged packages
6. Automatically generating audit reports

The result is a significantly faster and more standardized auditing process.

---

## Technologies

* Python
* PySide6
* SQLite
* PyMuPDF
* Pandas

---

## Setup

Create the environment using uv:

```bash
uv init .
uv venv
uv add pyside6 pymupdf pandas
```

Run the application:

```bash
uv run python main.py
```

---

## Current Features

### PDF Processing

* Open BuildingLink Event Log PDFs
* Automatically parse package entries
* Extract unit information
* Extract resident information
* Extract package information
* Extract tracking numbers
* Automatically determine tracking last four digits

### Audit Workflow

* One click package verification
* Search packages
* Filter unchecked packages
* Save audit state automatically
* Persistent audit progress
* Highlight verified packages

### Bulk Actions

* Mark All Visible
* Unmark All Visible

These actions respect:

* Search filters
* Unchecked Only filters

Keyboard shortcuts:

```text
Ctrl+A          Mark All Visible
Ctrl+Shift+A    Unmark All Visible
```

### Package Error Tracking

Supports spreadsheet style entry and bulk paste import.

Fields:

```text
Unit
Location
Carrier
Last 4
Error Note
```

### Double Logged Package Tracking

Supports spreadsheet style entry and bulk paste import.

Fields:

```text
Unit
Location
Carrier
Last 4
```

### Spreadsheet Style Entry

Supports:

* Tab navigation
* Direct cell editing
* Automatic blank row creation
* Dropdown selections

Locations:

```text
SHELF
BIN
BB
CG
UG
ALPHA
FCR
```

Carriers:

```text
USPS
UPS
FEDEX
AMZ
ONTRAC
DHL
PKG
KEY
FOOD
PHARMACY
```

### Export Options

* Audit TXT Report
* CSV Export
* Highlighted PDF Export

---

## Audit Report Format

### Section 1: Picked Up But Not Closed Out

Automatically generated from packages that remain unchecked during the audit.

Example:

```text
1708S | 7463
2205S | NaN
```

---

### Section 2: Package Errors

Manually entered package discrepancies.

Example:

```text
1708S | BIN | USPS | 8572 | Logged for wrong unit
1803S | BB | AMZ | 1968 | Package found but not logged
```

---

### Section 3: Double Logged Packages

Manually entered duplicate package records.

Example:

```text
0205S | BIN | FEDEX | 9669
3207S | CG | UPS | 1821
```

---

## Audit State Management

The application stores audit progress using a PDF hash.

This allows audits to be paused and resumed without losing progress.

### Clear Current Audit

Removes:

```text
Checked package status
Package errors
Double logged packages
```

for the currently loaded PDF.

### Clear Manual Sections

Removes:

```text
Package Errors
Double Logged Packages
```

while preserving package verification status.

---

## Version History

### Version 0.1

Initial proof of concept.

Features:

* PDF parsing
* Package extraction
* Click to verify packages

### Version 0.2

Refactored into modular architecture.

Added:

* models.py
* parser.py
* database.py
* audit_report.py
* gui.py

### Version 0.3

Added audit reporting features.

Added:

* Package Errors
* Double Logged Packages
* TXT report generation
* CSV export
* Highlighted PDF export

### Version 0.4

Added workflow improvements.

Added:

* Mark All Visible
* Unmark All Visible
* Keyboard shortcuts
* Spreadsheet style data entry
* Auto generated blank rows
* Dropdown selections
* Bulk paste support
* Audit reset functionality

---

## Impact

This tool was developed to reduce the amount of manual documentation required during package audits.

By converting audit observations directly into structured data, the application eliminates:

* Handwritten audit sheets
* Manual report writing
* Manual sorting of audit findings
* Repetitive transcription tasks

---

## Future Development Ideas

Potential future enhancements include:

### Audit Intelligence

* Automatic duplicate tracking number detection
* Automatic double log detection
* Suspicious package identification
* Missing package pattern detection

### Analytics

* Historical audit database
* Audit frequency reporting
* Package volume trends
* Carrier statistics
* Building level package metrics

### Workflow Improvements

* Keyboard driven audit mode
* Batch package actions
* Resident search enhancements
* Custom audit categories

### Deployment

* Standalone executable builds
* Multi user desktop deployment
* Local network web application
* Mobile friendly web interface

### Integrations

* BuildingLink API integration (if available)
* CSV import/export improvements
* Excel export support
* Email report generation

# Roadmap

This project began as a tool to automate BuildingLink package audits and reduce the manual work required to identify package discrepancies and generate audit reports.

Current development focuses on stability, usability, and real world testing.

---

## Version 0.4 (Current)

Current feature set includes:

* BuildingLink PDF parsing
* Package audit tracking
* Search and filtering
* Bulk audit actions
* Package error tracking
* Double logged package tracking
* Audit report generation
* CSV export
* Highlighted PDF export
* Audit persistence and recovery

This version is intended for active testing and workflow refinement.

---

## Version 1.0 (Planned)

The first stable production release.

Goals:

* Validate functionality through real audit usage
* Eliminate workflow issues discovered during testing
* Improve UI polish and reliability
* Improve parser accuracy and error handling
* Finalize audit report generation format
* Package application for non technical users

Version 1.0 represents the completion of the manual audit workflow.

No camera, OCR, machine learning, or BuildingLink integration is planned for the initial production release.

---

## Version 1.5 (Planned)

Quality of life improvements.

Potential additions:

* Improved report templates
* Audit history viewer
* Additional export formats
* Configurable report settings
* Improved search and filtering
* Enhanced package error management

---

## Version 2.0 (Planned)

Mobile assisted auditing.

Goals:

* Phone based barcode scanning
* Automatic package matching using tracking numbers
* Real time package verification
* Automatic package audit completion

Example workflow:

1. Open audit on desktop
2. Scan package barcode using phone
3. Matching package is identified
4. Package is automatically marked as audited

This version aims to significantly reduce audit completion time.

---

## Version 3.0 (Concept)

Shared web application.

Potential features:

* Centralized audit database
* Multi user support
* Tablet friendly interface
* Shared audit history
* Cloud deployment
* Management reporting

This version would allow multiple concierge staff members to use the system from a shared platform.

---

## Version 4.0 (Concept)

Intelligent package intake system.

Potential features:

* Barcode scanning
* OCR based label reading
* Automatic carrier detection
* Automatic tracking extraction
* Resident and unit matching
* Human in the Loop (HITL) confirmation workflow

Example workflow:

1. Scan package label
2. System extracts package information
3. User confirms results
4. Package is automatically logged

The objective is to reduce data entry while maintaining human verification.

---

## Version 5.0 (Long Term Vision)

BuildingLink integration and intelligent package processing.

Potential features:

* BuildingLink API integration
* Automatic package logging
* Resident lookup and validation
* Duplicate package detection
* Error prevention before package entry
* Continuous learning from user corrections and audit findings

Long term goal:

Transform package management from a manual data entry process into a scan first workflow where staff primarily verify information rather than enter it manually.

This project began as a personal productivity tool and may continue to evolve based on operational needs, user feedback, and real world usage.

