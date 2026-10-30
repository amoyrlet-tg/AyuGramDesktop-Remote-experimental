#pragma once

#include "core/core_settings_proxy.h"
#include "mtproto/details/mtproto_abstract_socket.h"
#include <QtCore/QJsonObject>

namespace MTP { class Instance; }

namespace Ayu::Remote::Runtime {

Core::SettingsProxy &Proxy();
QString CustomDeviceModel();
rpl::producer<QString> CustomDeviceModelChanges();
QString SystemLangCode();
QString CloudLangCode();
QString LangPackName();
QString RemoteConfig();
bool ImportArchive(MTP::Instance *instance, const QJsonObject &frame);
void RefreshProxy();
void BadConfiguration();
void WriteSettings();
void ApplyConfig(const MTPDconfig &config);
void ResetLanguage();
void RecordRetry(int code, const QString &type, uint32 constructor);
std::unique_ptr<MTP::details::AbstractSocket> WebSocket(
	not_null<QThread*> thread, const MTP::ProxyData &proxy);

} // namespace Ayu::Remote::Runtime
