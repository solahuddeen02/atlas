# Atlas

Atlas is a personal self-hosted ecosystem project inspired by Google-style services
(Drive, Photos, Notion-like apps), designed to be modular, extensible, and storage-centric.

> Status: Early development (private / experimental)

---

## 🎯 Vision

Build a unified ecosystem that:
- Stores **files, photos, and structured data** in one system
- Uses a **single object model** as the core
- Allows multiple apps (Drive, Photos, Notes, etc.) to be built on top
- Can be self-hosted and migrated between storage backends

This project is primarily for **learning, experimentation, and future expansion**.

---

## 🧱 Core Concepts

### Object-Centric Design
Everything in the system is an **object**:
- Files
- Photos
- Notes
- Future app entities

Each object contains:
- Metadata (type, name, timestamps)
- A reference to storage (not the storage itself)

### Separation of Concerns
- **API layer**: HTTP / FastAPI
- **Domain layer**: business logic
- **Storage layer**: file storage (local now, S3/MinIO later)
- **Database**: metadata only

---

## 🗂 Project Structure

atlas/
├── core/
│   ├── api/        # HTTP endpoints (FastAPI routers)
│   ├── domain/     # Core business logic (object model, rules)
│   ├── storage/    # Storage backends (local, future S3/MinIO)
│   └── db/         # Database utilities (connections, migrations)
│
├── data/           # Runtime file storage (ignored by git)
├── atlas.db        # Local metadata database (ignored by git)
├── README.md
└── .gitignore

---

## 🚀 Current Features

- Object creation
- File upload
- File download
- Local file storage
- SQLite metadata database
- Clean architecture baseline

---

## 🛣 Roadmap (Draft)

- [ ] Object listing & dashboard
- [ ] App separation (Drive / Photos)
- [ ] User & permission system
- [ ] Storage abstraction (S3 / MinIO)
- [ ] Plugin / app architecture
- [ ] UI frontend

---

## ⚠️ Disclaimer

This project is under active development.
APIs, data models, and structure may change at any time.

No license is provided at this stage.

---

## 📌 Notes

This repository is currently intended for personal use and learning.
Licensing and contribution guidelines will be decided later.
