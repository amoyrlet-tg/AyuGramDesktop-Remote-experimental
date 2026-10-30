#include "ayu/remote/mtp_runtime.h"

namespace Ayu::Remote::Runtime {

Core::SettingsProxy &Proxy() {
	static auto result = Core::SettingsProxy();
	return result;
}
QString CustomDeviceModel() { return u"AyuGram Remote VPS"_q; }
rpl::producer<QString> CustomDeviceModelChanges() { return rpl::never<QString>(); }
QString SystemLangCode() { return u"en"_q; }
QString CloudLangCode() { return u"en"_q; }
QString LangPackName() { return u"tdesktop"_q; }
QString RemoteConfig() { return {}; }
bool ImportArchive(MTP::Instance *, const QJsonObject &) { return false; }
void RefreshProxy() { QNetworkProxy::setApplicationProxy(QNetworkProxy::NoProxy); }
void BadConfiguration() { qFatal("MTProto configuration rejected."); }
void WriteSettings() {}
void ApplyConfig(const MTPDconfig &) {}
void ResetLanguage() {}
void RecordRetry(int, const QString &, uint32) {}
std::unique_ptr<MTP::details::AbstractSocket> WebSocket(
		not_null<QThread*>, const MTP::ProxyData &) {
	qFatal("Browser proxies are unavailable in the headless engine.");
}

} // namespace Ayu::Remote::Runtime

namespace Logs {
bool DebugEnabled() { return false; }
bool started() { return true; }
void writeMtp(int32, const QString &) {}
} // namespace Logs
