#include "ayu/remote/transport.h"

#include "ayu/remote/schema_fingerprint.h"
#include "ayu/remote/mtp_runtime.h"
#include "mtproto/facade.h"
#include "mtproto/mtp_instance.h"

#include <QtCore/QCryptographicHash>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QSaveFile>
#include <QtCore/QUuid>
#include <QtCore/QtEndian>

namespace Ayu::Remote {
namespace {

constexpr auto kMaxFrame = 24 * 1024 * 1024;
constexpr auto kMaxReply = 16 * 1024 * 1024;

bool Cacheable(mtpTypeId id) {
	switch (id) {
	case mtpc_help_getConfig:
	case mtpc_help_getAppConfig:
	case mtpc_help_getNearestDc:
	case mtpc_users_getUsers:
	case mtpc_users_getFullUser:
	case mtpc_contacts_getContacts:
	case mtpc_messages_getDialogs:
	case mtpc_messages_getHistory:
	case mtpc_messages_getMessages:
	case mtpc_messages_getPeerDialogs:
	case mtpc_messages_getChats:
	case mtpc_messages_getFullChat:
	case mtpc_messages_getDialogFilters:
	case mtpc_messages_getPinnedDialogs:
	case mtpc_channels_getChannels:
	case mtpc_channels_getFullChannel:
	case mtpc_channels_getMessages:
	case mtpc_photos_getUserPhotos:
	case mtpc_upload_getFile:
	case mtpc_upload_getWebFile:
	case mtpc_upload_getCdnFile:
	case mtpc_langpack_getLangPack:
	case mtpc_langpack_getDifference:
	case mtpc_langpack_getLanguages:
		return true;
	default:
		return false;
	}
}

} // namespace

Transport::Transport(MTP::Instance *instance, const QString &configPath)
: QObject(instance)
, _instance(instance)
, _configPath(configPath)
, _directory(QFileInfo(configPath).absolutePath() + u"/cache/"_q)
, _run(QUuid::createUuid().toString(QUuid::Id128)) {
	auto file = QFile(configPath);
	if (!file.open(QIODevice::ReadOnly) || file.size() > 65536) {
		qFatal("Remote profile cannot be opened.");
	}
	_config = QJsonDocument::fromJson(file.readAll()).object();
	for (const auto key : { "socket", "token", "client" }) {
		if (_config.value(QLatin1String(key)).toString().isEmpty()) {
			qFatal("Incomplete remote profile.");
		}
	}
	if (!QDir().mkpath(_directory + u"events"_q)) {
		qFatal("Remote cache cannot be created.");
	}
	_epoch = _config.value(u"epoch"_q).toString();
	if (!QDir().mkpath(_directory + u"archive"_q)) { qFatal("Cannot create archive cache."); }
	auto archiveCursor = QFile(_directory + u"archive-cursor"_q);
	if (archiveCursor.open(QIODevice::ReadOnly)) { _archiveCursor = archiveCursor.readAll().toULongLong(); }
	for (const auto &name : QDir(_directory + u"archive"_q).entryList(QDir::Files)) {
		auto valid = false;
		const auto seq = name.toULongLong(&valid);
		if (!valid || seq <= _archiveCursor) { continue; }
		auto file = QFile(_directory + u"archive/"_q + name);
		if (file.open(QIODevice::ReadOnly) && file.size() <= kMaxFrame) {
			_archive.emplace(seq, QJsonDocument::fromJson(file.readAll()).object());
		}
	}
	auto cursor = QFile(_directory + u"cursor"_q);
	if (cursor.open(QIODevice::ReadOnly)) {
		_cursor = QString::fromLatin1(cursor.readAll());
	}
	connect(&_socket, &QLocalSocket::connected, this, [=] {
		write({
			{ u"type"_q, u"hello"_q }, { u"version"_q, 1 },
			{ u"schema"_q, QString::fromLatin1(kSchemaFingerprint) },
			{ u"token"_q, _config.value(u"token"_q) },
			{ u"client"_q, _config.value(u"client"_q) },
			{ u"epoch"_q, _epoch }, { u"after"_q, _cursor },
			{ u"archive_after"_q, QString::number(_archiveCursor) },
		});
	});
	connect(&_socket, &QLocalSocket::readyRead, this, [=] { receive(); });
	connect(&_socket, &QLocalSocket::disconnected, this, [=] { disconnected(); });
	connect(&_socket, &QLocalSocket::errorOccurred, this, [=] { disconnected(); });
	_reconnect.setInterval(3000);
	connect(&_reconnect, &QTimer::timeout, this, [=] {
		if (_socket.state() == QLocalSocket::UnconnectedState) {
			_socket.connectToServer(_config.value(u"socket"_q).toString());
		}
	});
	_heartbeat.setInterval(30000);
	connect(&_heartbeat, &QTimer::timeout, this, [=] {
		if (_ready) { write({ { u"type"_q, u"ping"_q } }); }
	});
	_reconnect.start();
	_heartbeat.start();
	_archiveTimer.setInterval(1000);
	connect(&_archiveTimer, &QTimer::timeout, this, [=] { flushArchive(); });
	_archiveTimer.start();
	QTimer::singleShot(0, this, [=] { restart(); });
}

void Transport::restart() {
	_socket.abort();
	disconnected();
	_socket.connectToServer(_config.value(u"socket"_q).toString());
}

bool Transport::connected() const { return _ready; }

void Transport::write(const QJsonObject &frame) {
	const auto data = QJsonDocument(frame).toJson(QJsonDocument::Compact) + '\n';
	if (data.size() > kMaxFrame || _socket.bytesToWrite() > 2 * kMaxFrame) {
		_socket.abort();
		return;
	}
	_socket.write(data);
}

QString Transport::cacheKey(const QByteArray &body, int dc) const {
	const auto bytes = QByteArray::number(dc) + ':' + body;
	return QString::fromLatin1(QCryptographicHash::hash(bytes, QCryptographicHash::Sha256).toHex());
}

QByteArray Transport::cached(const QString &key) const {
	if (key.isEmpty()) { return {}; }
	auto file = QFile(_directory + key);
	return file.open(QIODevice::ReadOnly) && file.size() <= kMaxReply ? file.readAll() : QByteArray();
}

void Transport::save(const QString &path, const QByteArray &data) {
	auto file = QSaveFile(path);
	if (!file.open(QIODevice::WriteOnly)) { qFatal("Cannot persist remote cache."); }
	file.setPermissions(QFile::ReadOwner | QFile::WriteOwner);
	if (file.write(data) != data.size() || !file.commit()) {
		qFatal("Cannot commit remote cache.");
	}
}

void Transport::send(mtpRequestId id, MTP::details::SerializedRequest request,
		MTP::ResponseHandler callbacks, MTP::ShiftedDcId dc, mtpRequestId after) {
	const auto position = MTP::details::SerializedRequest::kMessageBodyPosition;
	const auto body = QByteArray(reinterpret_cast<const char*>(request->constData() + position),
		(request->size() - position) * sizeof(mtpPrime));
	const auto wireId = _run + '-' + QString::number(id);
	const auto constructor = qFromLittleEndian<quint32>(body.constData());
	auto pending = Pending{ id, std::move(callbacks), Cacheable(constructor) ? cacheKey(body, dc) : QString() };
	if (!_ready) {
		const auto saved = cached(pending.cacheKey);
		_pending.emplace(wireId, std::move(pending));
		QTimer::singleShot(0, this, [=] {
			if (!saved.isEmpty()) {
				finish(wireId, saved);
			} else if (const auto i = _pending.find(wireId); i != _pending.end()) {
				auto value = std::move(i->second);
				_pending.erase(i);
				localError(std::move(value), u"REMOTE_OFFLINE"_q);
			}
		});
		return;
	}
	if (_pending.size() >= 256) {
		localError(std::move(pending), u"REMOTE_BUSY"_q);
		return;
	}
	_pending.emplace(wireId, std::move(pending));
	write({
		{ u"type"_q, u"rpc"_q }, { u"id"_q, wireId },
		{ u"body"_q, QString::fromLatin1(body.toBase64()) }, { u"dc"_q, dc },
		{ u"after"_q, after ? _run + '-' + QString::number(after) : QString() },
	});
}

void Transport::cancel(mtpRequestId id) {
	_pending.erase(_run + '-' + QString::number(id));
}

bool Transport::hasCallback(mtpRequestId id) const {
	return _pending.contains(_run + '-' + QString::number(id));
}

void Transport::localError(Pending pending, const QString &error) {
	auto response = MTP::Response();
	response.requestId = pending.id;
	MTPRpcError(MTP_rpc_error(MTP_int(400), MTP_string(error))).write(response.reply);
	if (pending.callbacks.fail) {
		pending.callbacks.fail(MTP::Error(response.reply), response);
	}
}

void Transport::finish(const QString &id, const QByteArray &bytes) {
	const auto i = _pending.find(id);
	if (i == _pending.end()) { return; }
	auto pending = std::move(i->second);
	_pending.erase(i);
	if (bytes.isEmpty() || bytes.size() > kMaxReply || bytes.size() % 4) {
		localError(std::move(pending), u"REMOTE_BAD_RESPONSE"_q);
		return;
	}
	auto response = MTP::Response();
	response.requestId = pending.id;
	response.reply.resize(bytes.size() / sizeof(mtpPrime));
	memcpy(response.reply.data(), bytes.constData(), bytes.size());
	if (mtpTypeId(response.reply.front()) == mtpc_rpc_error) {
		if (pending.callbacks.fail) {
			pending.callbacks.fail(MTP::Error(response.reply), response);
		}
	} else {
		if (!pending.cacheKey.isEmpty()) { save(_directory + pending.cacheKey, bytes); }
		if (pending.callbacks.done && !pending.callbacks.done(response)) {
			localError(std::move(pending), u"REMOTE_PARSE_FAILED"_q);
		}
	}
}

void Transport::receive() {
	_buffer += _socket.readAll();
	if (_buffer.size() > 2 * kMaxFrame) { _socket.abort(); return; }
	while (true) {
		const auto newline = _buffer.indexOf('\n');
		if (newline < 0) {
			if (_buffer.size() > kMaxFrame) { _socket.abort(); }
			return;
		}
		if (newline > kMaxFrame) { _socket.abort(); return; }
		const auto document = QJsonDocument::fromJson(_buffer.left(newline));
		_buffer.remove(0, newline + 1);
		if (!document.isObject()) { _socket.abort(); return; }
		const auto guard = QPointer<Transport>(this);
		handle(document.object());
		if (!guard) { return; }
	}
}

void Transport::handle(const QJsonObject &frame) {
	const auto type = frame.value(u"type"_q).toString();
	if (type == u"hello"_q) {
		const auto epoch = frame.value(u"epoch"_q).toString();
		if (frame.value(u"version"_q).toInt() != 1
			|| frame.value(u"schema"_q).toString() != QString::fromLatin1(kSchemaFingerprint)
			|| epoch.isEmpty() || (!_epoch.isEmpty() && epoch != _epoch)) {
			qFatal("Remote server identity or schema mismatch.");
		}
		_epoch = epoch;
		_config.insert(u"epoch"_q, epoch);
		save(_configPath, QJsonDocument(_config).toJson());
		_ready = true;
		_instance->requestConfig();
		const auto guard = QPointer<Transport>(this);
		_instance->onStateChange(_instance->mainDcId(), MTP::ConnectedState);
		if (!guard) { return; }
		_instance->onSessionReset(_instance->mainDcId());
	} else if (!_ready) {
		_socket.abort();
	} else if (type == u"result"_q) {
		const auto bytes = QByteArray::fromBase64(frame.value(u"body"_q).toString().toLatin1(),
			QByteArray::AbortOnBase64DecodingErrors);
		finish(frame.value(u"id"_q).toString(), bytes);
	} else if (type == u"archive"_q) {
		auto valid = false;
		const auto seq = frame.value(u"seq"_q).toString().toULongLong(&valid);
		if (!valid || !seq) { _socket.abort(); return; }
		if (seq > _archiveCursor) {
			save(_directory + u"archive/"_q + QString::number(seq), QJsonDocument(frame).toJson());
			_archive[seq] = frame;
			flushArchive();
		}
	} else if (type == u"update"_q) {
		const auto seq = frame.value(u"seq"_q).toString();
		auto valid = false;
		const auto value = seq.toULongLong(&valid);
		const auto bytes = QByteArray::fromBase64(frame.value(u"body"_q).toString().toLatin1(),
			QByteArray::AbortOnBase64DecodingErrors);
		if (!valid || value != _cursor.toULongLong() + 1 || bytes.isEmpty()
			|| bytes.size() > kMaxReply || bytes.size() % 4) {
			_socket.abort();
			return;
		}
		save(_directory + u"events/"_q + QString::number(value), bytes);
		auto response = MTP::Response();
		response.reply.resize(bytes.size() / sizeof(mtpPrime));
		memcpy(response.reply.data(), bytes.constData(), bytes.size());
		const auto guard = QPointer<Transport>(this);
		_instance->processUpdate(response);
		if (!guard) { return; }
		_cursor = QString::number(value);
		save(_directory + u"cursor"_q, _cursor.toLatin1());
	}
}

void Transport::flushArchive() {
	for (auto count = 0; count != 32; ++count) {
		const auto i = _archive.find(_archiveCursor + 1);
		if (i == _archive.end() || !Runtime::ImportArchive(_instance, i->second)) { return; }
		++_archiveCursor;
		save(_directory + u"archive-cursor"_q, QByteArray::number(_archiveCursor));
		_archive.erase(i);
	}
}

void Transport::disconnected() {
	const auto wasReady = _ready;
	_ready = false;
	_buffer.clear();
	auto pending = std::move(_pending);
	_pending.clear();
	const auto guard = QPointer<Transport>(this);
	if (wasReady) { _instance->onStateChange(_instance->mainDcId(), MTP::DisconnectedState); }
	if (!guard) { return; }
	for (auto &[id, value] : pending) {
		localError(std::move(value), u"REMOTE_OUTCOME_UNKNOWN"_q);
		if (!guard) { return; }
	}
}

} // namespace Ayu::Remote
