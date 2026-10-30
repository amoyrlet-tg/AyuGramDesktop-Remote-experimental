#include "ayu/remote/mtp_runtime.h"

#include "ayu/data/ayu_database.h"
#include "core/application.h"
#include "main/main_account.h"
#include "main/main_domain.h"
#include "main/main_session.h"

namespace Ayu::Remote::Runtime {

bool ImportArchive(MTP::Instance *instance, const QJsonObject &frame) {
	auto session = static_cast<Main::Session*>(nullptr);
	for (const auto &[index, account] : Core::App().domain().accounts()) {
		if (account->maybeSession() && &account->mtp() == instance) {
			session = account->maybeSession();
			break;
		}
	}
	if (!session) { return false; }
	const auto data = frame.value(u"message"_q).toObject();
	const auto peer = data.value(u"peer"_q).toString();
	const auto parts = peer.split(':');
	if (parts.size() != 2) { return false; }
	auto valid = false;
	const auto peerId = parts[1].toLongLong(&valid);
	if (!valid || peerId <= 0) { return false; }
	auto message = AyuMessageBase{};
	message.userId = session->userId().bare;
	message.peerId = peerId;
	message.dialogId = parts[0] == u"user"_q ? peerId : -peerId;
	const auto from = data.value(u"from"_q).toString();
	message.fromId = from == u"self"_q ? message.userId : from.section(':', 1).toLongLong();
	if (!message.fromId) { message.fromId = peerId; }
	message.messageId = data.value(u"id"_q).toInt();
	message.date = data.value(u"date"_q).toInt();
	message.editDate = data.value(u"edit_date"_q).toInt();
	message.entityCreateDate = frame.value(u"created"_q).toInt();
	message.groupedId = data.value(u"grouped"_q).toString().toLongLong();
	message.topicId = data.value(u"topic"_q).toInt();
	message.text = data.value(u"text"_q).toString().toStdString();
	message.postAuthor = data.value(u"post_author"_q).toString().toStdString();
	const auto entities = QByteArray::fromBase64(data.value(u"entities"_q).toString().toLatin1(),
		QByteArray::AbortOnBase64DecodingErrors);
	message.textEntities.assign(entities.cbegin(), entities.cend());
	message.mediaPath = "/";
	if (message.text.empty()) { return true; }
	return AyuDatabase::importRemoteMessage(message, frame.value(u"kind"_q).toString() == u"deleted"_q);
}

} // namespace Ayu::Remote::Runtime
