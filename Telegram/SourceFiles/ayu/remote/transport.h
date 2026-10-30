#pragma once

#include "mtproto/mtproto_response.h"
#include "mtproto/details/mtproto_serialized_request.h"

#include <QtCore/QJsonObject>
#include <QtCore/QTimer>
#include <QtNetwork/QLocalSocket>

namespace MTP { class Instance; }

namespace Ayu::Remote {

class Transport final : public QObject {
public:
	Transport(MTP::Instance *instance, const QString &configPath);
	void send(mtpRequestId id, MTP::details::SerializedRequest request,
		MTP::ResponseHandler callbacks, MTP::ShiftedDcId dc, mtpRequestId after);
	void cancel(mtpRequestId id);
	bool hasCallback(mtpRequestId id) const;
	bool connected() const;
	void restart();

private:
	struct Pending {
		mtpRequestId id = 0;
		MTP::ResponseHandler callbacks;
		QString cacheKey;
	};
	void receive();
	void handle(const QJsonObject &frame);
	void write(const QJsonObject &frame);
	void disconnected();
	void flushArchive();
	void finish(const QString &id, const QByteArray &reply);
	void localError(Pending pending, const QString &error);
	QString cacheKey(const QByteArray &body, int dc) const;
	QByteArray cached(const QString &key) const;
	void save(const QString &path, const QByteArray &data);

	MTP::Instance *_instance = nullptr;
	QLocalSocket _socket;
	QTimer _reconnect;
	QTimer _heartbeat;
	QTimer _archiveTimer;
	QJsonObject _config;
	QString _configPath;
	QString _directory;
	QString _epoch;
	QString _cursor = u"0"_q;
	quint64 _archiveCursor = 0;
	std::map<quint64, QJsonObject> _archive;
	QString _run;
	QByteArray _buffer;
	std::map<QString, Pending> _pending;
	bool _ready = false;

};

} // namespace Ayu::Remote
