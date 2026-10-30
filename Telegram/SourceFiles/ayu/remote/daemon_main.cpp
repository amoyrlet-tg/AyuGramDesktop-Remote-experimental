#include "ayu/remote/schema_fingerprint.h"

#include "base/integration.h"
#include "mtproto/mtp_instance.h"
#include "mtproto/mtproto_auth_key.h"
#include "mtproto/mtproto_config.h"
#include "mtproto/sender.h"

#include <QtCore/QSaveFile>
#include <QtCore/QSocketNotifier>
#include <cerrno>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace Ayu::Remote {
namespace {

constexpr auto kMaxFrame = 24 * 1024 * 1024;
constexpr auto kMaxOutput = 96 * 1024 * 1024;
constexpr auto kMaxPayload = 16 * 1024 * 1024;

class Integration final : public base::Integration {
public:
	Integration(int argc, char **argv) : base::Integration(argc, argv) {}
	void enterFromEventLoop(FnMut<void()> &&method) override { method(); }
	bool logSkipDebug() override { return true; }
	void logMessageDebug(const QString &) override {}
	void logMessage(const QString &) override {}

};

QByteArray Raw(const mtpBuffer &buffer) {
	return QByteArray(reinterpret_cast<const char*>(buffer.constData()), buffer.size() * sizeof(mtpPrime));
}

template <typename T>
QString Encoded(const T &value) {
	auto buffer = mtpBuffer();
	value.write(buffer);
	return QString::fromLatin1(Raw(buffer).toBase64());
}

QString Peer(const MTPPeer &peer) {
	return peer.match([](const MTPDpeerUser &d) {
		return u"user:"_q + QString::number(d.vuser_id().v);
	}, [](const MTPDpeerChat &d) {
		return u"chat:"_q + QString::number(d.vchat_id().v);
	}, [](const MTPDpeerChannel &d) {
		return u"channel:"_q + QString::number(d.vchannel_id().v);
	});
}

void Message(const MTPMessage &message, QJsonArray &out) {
	message.match([&](const MTPDmessage &d) {
		out.append(QJsonObject{
			{ u"peer"_q, Peer(d.vpeer_id()) }, { u"id"_q, d.vid().v },
			{ u"date"_q, d.vdate().v }, { u"text"_q, qs(d.vmessage()) },
			{ u"edit_date"_q, d.vedit_date().value_or_empty().v },
			{ u"from"_q, d.vfrom_id() ? Peer(*d.vfrom_id()) : d.is_out() ? u"self"_q : Peer(d.vpeer_id()) },
			{ u"post_author"_q, qs(d.vpost_author().value_or_empty()) },
			{ u"grouped"_q, QString::number(d.vgrouped_id().value_or_empty().v) },
			{ u"entities"_q, Encoded(d.ventities().value_or_empty()) },
			{ u"tl"_q, Encoded(message) },
		});
	}, [&](const MTPDmessageService &d) {
		out.append(QJsonObject{
			{ u"peer"_q, Peer(d.vpeer_id()) }, { u"id"_q, d.vid().v },
			{ u"date"_q, d.vdate().v }, { u"tl"_q, Encoded(message) },
		});
	}, [](const auto &) {});
}

void Update(const MTPUpdate &update, QJsonArray &messages, QJsonArray &deleted) {
	update.match([&](const MTPDupdateNewMessage &d) { Message(d.vmessage(), messages); },
		[&](const MTPDupdateNewChannelMessage &d) { Message(d.vmessage(), messages); },
		[&](const MTPDupdateEditMessage &d) { Message(d.vmessage(), messages); },
		[&](const MTPDupdateEditChannelMessage &d) { Message(d.vmessage(), messages); },
		[&](const MTPDupdateDeleteMessages &d) {
			auto ids = QJsonArray();
			for (const auto &id : d.vmessages().v) { ids.append(id.v); }
			deleted.append(QJsonObject{ { u"peer"_q, u"nonchannel"_q }, { u"ids"_q, ids } });
		}, [&](const MTPDupdateDeleteChannelMessages &d) {
			auto ids = QJsonArray();
			for (const auto &id : d.vmessages().v) { ids.append(id.v); }
			deleted.append(QJsonObject{
				{ u"peer"_q, u"channel:"_q + QString::number(d.vchannel_id().v) }, { u"ids"_q, ids },
			});
		}, [](const auto &) {});
}

void Collect(const MTP::Response &response, QJsonObject &out) {
	if (response.reply.isEmpty()) { return; }
	const auto finish = response.reply.constData() + response.reply.size();
	auto from = response.reply.constData();
	auto messages = QJsonArray();
	auto deleted = QJsonArray();
	switch (mtpTypeId(*from)) {
	case mtpc_auth_authorization: {
		auto result = MTPauth_Authorization();
		if (!result.read(from, finish)) { break; }
		result.match([&](const MTPDauth_authorization &d) {
			if (d.vuser().type() == mtpc_user) {
				out.insert(u"account"_q, QString::number(d.vuser().c_user().vid().v));
			}
		}, [](const auto &) {});
	} break;
	case mtpc_updates:
	case mtpc_updatesCombined:
	case mtpc_updateShort:
	case mtpc_updateShortMessage:
	case mtpc_updateShortChatMessage:
	case mtpc_updatesTooLong: {
		auto result = MTPUpdates();
		if (!result.read(from, finish)) { break; }
		result.match([&](const MTPDupdateShort &d) { Update(d.vupdate(), messages, deleted); },
			[&](const MTPDupdateShortMessage &d) {
				messages.append(QJsonObject{
					{ u"peer"_q, u"user:"_q + QString::number(d.vuser_id().v) },
					{ u"id"_q, d.vid().v }, { u"text"_q, qs(d.vmessage()) },
					{ u"from"_q, d.is_out() ? u"self"_q : u"user:"_q + QString::number(d.vuser_id().v) },
					{ u"entities"_q, Encoded(d.ventities().value_or_empty()) },
					{ u"date"_q, d.vdate().v }, { u"short_tl"_q, Encoded(result) },
				});
			}, [&](const MTPDupdateShortChatMessage &d) {
				messages.append(QJsonObject{
					{ u"peer"_q, u"chat:"_q + QString::number(d.vchat_id().v) },
					{ u"id"_q, d.vid().v }, { u"text"_q, qs(d.vmessage()) },
					{ u"from"_q, u"user:"_q + QString::number(d.vfrom_id().v) },
					{ u"entities"_q, Encoded(d.ventities().value_or_empty()) },
					{ u"date"_q, d.vdate().v }, { u"short_tl"_q, Encoded(result) },
				});
			}, [&](const auto &d) {
				if constexpr (requires { d.vupdates(); }) {
					for (const auto &u : d.vupdates().v) { Update(u, messages, deleted); }
				}
			});
	} break;
	case mtpc_messages_messages:
	case mtpc_messages_messagesSlice:
	case mtpc_messages_channelMessages: {
		auto result = MTPmessages_Messages();
		if (!result.read(from, finish)) { break; }
		result.match([&](const auto &d) {
			if constexpr (requires { d.vmessages(); }) {
				for (const auto &m : d.vmessages().v) { Message(m, messages); }
			}
		});
	} break;
	case mtpc_messages_dialogs:
	case mtpc_messages_dialogsSlice: {
		auto result = MTPmessages_Dialogs();
		if (!result.read(from, finish)) { break; }
		result.match([&](const auto &d) {
			if constexpr (requires { d.vmessages(); }) {
				for (const auto &m : d.vmessages().v) { Message(m, messages); }
			}
		});
	} break;
	default: break;
	}
	out.insert(u"messages"_q, messages);
	out.insert(u"deleted"_q, deleted);
}

void Write(QJsonObject frame) {
	const auto data = QJsonDocument(frame).toJson(QJsonDocument::Compact) + '\n';
	if (data.size() > kMaxOutput) { qFatal("Engine output exceeds frame limit."); }
	auto offset = qsizetype(0);
	while (offset < data.size()) {
		const auto written = ::write(STDOUT_FILENO, data.constData() + offset, data.size() - offset);
		if (written < 0 && errno == EINTR) { continue; }
		if (written <= 0) { QCoreApplication::exit(1); return; }
		offset += written;
	}
}

QJsonObject Checkpoint(const MTPupdates_State &state) {
	const auto &d = state.data();
	return { { u"pts"_q, d.vpts().v }, { u"qts"_q, d.vqts().v },
		{ u"date"_q, d.vdate().v }, { u"seq"_q, d.vseq().v } };
}

class Synchronizer final : public QObject {
public:
	explicit Synchronizer(MTP::Instance *instance);
	void initialize(QJsonObject state);
	void commit(const QJsonObject &state);

private:
	void poll();
	void publish(QJsonObject state, QJsonArray messages = {}, QJsonArray deleted = {}, bool notify = true);
	void difference(const MTPupdates_Difference &result);

	MTP::Sender _sender;
	QTimer _timer;
	QJsonObject _state;
	QJsonObject _proposed;
	bool _busy = false;
	bool _started = false;
	bool _gap = false;

};

Synchronizer::Synchronizer(MTP::Instance *instance)
: QObject(instance)
, _sender(instance) {
	_timer.setInterval(5000);
	connect(&_timer, &QTimer::timeout, this, [=] { poll(); });
}

void Synchronizer::initialize(QJsonObject state) {
	if (_started) { qFatal("Sync initialized twice."); }
	_started = true;
	_state = std::move(state);
	_timer.start();
	poll();
}

void Synchronizer::commit(const QJsonObject &state) {
	if (!_busy || _proposed.isEmpty() || state != _proposed) {
		qFatal("Sync commit mismatch.");
	}
	_state = std::move(_proposed);
	_proposed = {};
	_busy = false;
}

void Synchronizer::poll() {
	if (_busy || !_started || _gap) { return; }
	_busy = true;
	const auto fail = [=](const MTP::Error &) { _busy = false; };
	if (_state.isEmpty()) {
		_sender.request(MTPupdates_GetState()).done([=](const MTPupdates_State &state) {
			publish(Checkpoint(state), {}, {}, false);
		}).fail(fail).send();
	} else {
		_sender.request(MTPupdates_GetDifference(MTP_flags(0),
			MTP_int(_state.value(u"pts"_q).toInt()), MTP_int(0), MTP_int(0),
			MTP_int(_state.value(u"date"_q).toInt()), MTP_int(_state.value(u"qts"_q).toInt()),
			MTP_int(0))).done([=](const MTPupdates_Difference &result) {
			difference(result);
		}).fail(fail).send();
	}
}

void Synchronizer::publish(QJsonObject state, QJsonArray messages, QJsonArray deleted, bool notify) {
	_proposed = state;
	Write({ { u"type"_q, u"update"_q }, { u"body"_q, Encoded(MTPUpdates(MTP_updatesTooLong())) },
		{ u"messages"_q, messages }, { u"deleted"_q, deleted },
		{ u"checkpoint"_q, state }, { u"notify"_q, notify } });
}

void Synchronizer::difference(const MTPupdates_Difference &result) {
	result.match([&](const MTPDupdates_differenceEmpty &d) {
		auto state = _state;
		state.insert(u"date"_q, d.vdate().v);
		state.insert(u"seq"_q, d.vseq().v);
		publish(state, {}, {}, false);
	}, [&](const MTPDupdates_differenceTooLong &) {
		_gap = true;
		_busy = false;
		Write({ { u"type"_q, u"update"_q }, { u"body"_q, Encoded(MTPUpdates(MTP_updatesTooLong())) },
			{ u"gap"_q, true } });
	}, [&](const auto &d) {
		auto messages = QJsonArray();
		auto deleted = QJsonArray();
		for (const auto &message : d.vnew_messages().v) { Message(message, messages); }
		for (const auto &update : d.vother_updates().v) { Update(update, messages, deleted); }
		if constexpr (requires { d.vstate(); }) {
			publish(Checkpoint(d.vstate()), messages, deleted);
		} else {
			publish(Checkpoint(d.vintermediate_state()), messages, deleted);
		}
	});
}

MTP::Instance::Fields Load(const QString &directory) {
	auto result = MTP::Instance::Fields();
	result.config = std::make_unique<MTP::Config>(MTP::Environment::Production);
	result.deviceModel = u"AyuGram Remote VPS"_q;
	result.systemVersion = QSysInfo::prettyProductName();
	auto file = QFile(directory + u"/keys"_q);
	if (!file.exists()) { return result; }
	if (!file.open(QIODevice::ReadOnly) || file.size() > 4 * 1024 * 1024) {
		qFatal("Cannot read engine keys.");
	}
	auto stream = QDataStream(file.readAll());
	stream.setVersion(QDataStream::Qt_5_1);
	auto version = quint32(0);
	auto count = quint32(0);
	auto config = QByteArray();
	stream >> version >> result.mainDcId >> count;
	if (version != 1 || count > 32) { qFatal("Invalid engine key file."); }
	for (auto i = quint32(0); i != count; ++i) {
		auto dc = qint32(0);
		auto bytes = QByteArray();
		stream >> dc >> bytes;
		if (bytes.size() != MTP::AuthKey::kSize) { qFatal("Invalid engine key."); }
		auto key = MTP::AuthKey::Data();
		memcpy(key.data(), bytes.constData(), bytes.size());
		result.keys.push_back(std::make_shared<MTP::AuthKey>(MTP::AuthKey::Type::ReadFromFile, dc, key));
	}
	stream >> config;
	if (stream.status() != QDataStream::Ok || !stream.atEnd()) { qFatal("Corrupt engine key file."); }
	if (auto saved = MTP::Config::FromSerialized(config)) { result.config = std::move(saved); }
	return result;
}

void Save(const QString &directory, MTP::Instance &instance) {
	auto file = QSaveFile(directory + u"/keys"_q);
	if (!file.open(QIODevice::WriteOnly)) { qFatal("Cannot persist engine keys."); }
	file.setPermissions(QFile::ReadOwner | QFile::WriteOwner);
	auto stream = QDataStream(&file);
	stream.setVersion(QDataStream::Qt_5_1);
	const auto keys = instance.getKeysForWrite();
	stream << quint32(1) << qint32(instance.mainDcId()) << quint32(keys.size());
	for (const auto &key : keys) {
		const auto data = key->data();
		stream << qint32(key->dcId()) << QByteArray(reinterpret_cast<const char*>(data.data()), data.size());
	}
	stream << instance.config().serialize();
	if (stream.status() != QDataStream::Ok || !file.commit()) { qFatal("Cannot commit engine keys."); }
}

void Request(MTP::Instance &instance, std::map<QString, mtpRequestId> &pending, const QJsonObject &frame) {
	if (frame.value(u"type"_q).toString() != u"rpc"_q) { qFatal("Unsupported engine command."); }
	const auto id = frame.value(u"id"_q).toString();
	const auto bytes = QByteArray::fromBase64(frame.value(u"body"_q).toString().toLatin1(),
		QByteArray::AbortOnBase64DecodingErrors);
	if (id.isEmpty() || bytes.isEmpty() || bytes.size() > kMaxPayload || bytes.size() % 4) {
		qFatal("Invalid engine request.");
	}
	auto request = MTP::details::SerializedRequest::Prepare(bytes.size() / sizeof(mtpPrime));
	const auto position = request->size();
	request->resize(position + bytes.size() / sizeof(mtpPrime));
	memcpy(request->data() + position, bytes.constData(), bytes.size());
	const auto requestId = MTP::details::GetNextRequestId();
	if (!pending.emplace(id, requestId).second) { qFatal("Duplicate engine request."); }
	const auto done = [=, &pending](const MTP::Response &response) {
		pending.erase(id);
		auto frame = QJsonObject{
			{ u"type"_q, u"result"_q }, { u"id"_q, id },
			{ u"body"_q, QString::fromLatin1(Raw(response.reply).toBase64()) },
		};
		Collect(response, frame);
		Write(frame);
		return true;
	};
	const auto fail = [=](const MTP::Error &error, const MTP::Response &response) {
		if (MTP::IsTemporaryError(error)) { return false; }
		auto result = response;
		result.reply.clear();
		MTPRpcError(MTP_rpc_error(MTP_int(error.code()), MTP_string(error.type()))).write(result.reply);
		return done(result);
	};
	instance.sendSerialized(requestId, std::move(request),
		MTP::ResponseHandler{ done, fail }, frame.value(u"dc"_q).toInt(), 0, 0);
}

} // namespace
} // namespace Ayu::Remote

int main(int argc, char **argv) {
	using namespace Ayu::Remote;
	::umask(0077);
	auto app = QCoreApplication(argc, argv);
	auto integration = Integration(argc, argv);
	base::Integration::Set(&integration);
	crl::details::init();
	crl::init_main_queue([](void (*callable)(void*), void *argument) {
		QMetaObject::invokeMethod(QCoreApplication::instance(), [=] { callable(argument); }, Qt::QueuedConnection);
	});
	const auto args = app.arguments();
	if (args.size() != 3 || args[1] != u"--state"_q) { return 2; }
	const auto directory = args[2];
	if (!QDir().mkpath(directory)) { return 2; }
	auto instance = MTP::Instance(MTP::Instance::Mode::Normal, Load(directory));
	instance.writeKeysRequests() | rpl::on_next([&] { Save(directory, instance); }, instance.lifetime());
	instance.config().updates() | rpl::on_next([&] { Save(directory, instance); }, instance.lifetime());
	instance.setUpdatesHandler([&](const MTP::Response &response) {
		auto frame = QJsonObject{
			{ u"type"_q, u"update"_q },
			{ u"body"_q, QString::fromLatin1(Raw(response.reply).toBase64()) },
		};
		Collect(response, frame);
		Write(frame);
	});
	auto sync = Synchronizer(&instance);
	const auto flags = fcntl(STDIN_FILENO, F_GETFL);
	if (flags < 0 || fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK) < 0) { return 2; }
	auto notifier = QSocketNotifier(STDIN_FILENO, QSocketNotifier::Read);
	auto buffer = QByteArray();
	auto pending = std::map<QString, mtpRequestId>();
	QObject::connect(&notifier, &QSocketNotifier::activated, &app, [&] {
		auto chunk = std::array<char, 65536>();
		while (true) {
			const auto size = ::read(STDIN_FILENO, chunk.data(), chunk.size());
			if (size < 0 && errno == EINTR) { continue; }
			if (size < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) { break; }
			if (size <= 0) { app.quit(); return; }
			buffer.append(chunk.data(), size);
			if (buffer.size() > kMaxFrame) { qFatal("Engine frame too large."); }
			while (true) {
				const auto newline = buffer.indexOf('\n');
				if (newline < 0) { break; }
				const auto document = QJsonDocument::fromJson(buffer.left(newline));
				buffer.remove(0, newline + 1);
				if (!document.isObject()) { qFatal("Invalid engine JSON."); }
				const auto frame = document.object();
				const auto type = frame.value(u"type"_q).toString();
				if (type == u"sync"_q) {
					sync.initialize(frame.value(u"state"_q).toObject());
				} else if (type == u"commit"_q) {
					sync.commit(frame.value(u"state"_q).toObject());
				} else if (type == u"cancel"_q) {
					const auto i = pending.find(frame.value(u"id"_q).toString());
					if (i != pending.end()) {
						instance.cancel(i->second);
						pending.erase(i);
					}
				} else {
					Request(instance, pending, frame);
				}
			}
		}
	});
	Write({ { u"type"_q, u"ready"_q }, { u"version"_q, 1 },
		{ u"schema"_q, QString::fromLatin1(kSchemaFingerprint) } });
	const auto result = app.exec();
	Save(directory, instance);
	return result;
}
