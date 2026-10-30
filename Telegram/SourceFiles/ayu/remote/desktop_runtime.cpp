#include "ayu/remote/mtp_runtime.h"

#include "core/application.h"
#include "core/core_settings.h"
#include "lang/lang_cloud_manager.h"
#include "lang/lang_instance.h"
#include "mtproto/details/mtproto_web_proxy_socket.h"
#include "storage/localstorage.h"
#include "test/test_rpc_retry.h"

namespace Ayu::Remote::Runtime {

Core::SettingsProxy &Proxy() { return Core::App().settings().proxy(); }
QString CustomDeviceModel() { return Core::App().settings().customDeviceModel(); }
rpl::producer<QString> CustomDeviceModelChanges() {
	return Core::App().settings().customDeviceModelChanges();
}
QString SystemLangCode() { return Lang::GetInstance().systemLangCode(); }
QString CloudLangCode() { return Lang::GetInstance().cloudLangCode(Lang::Pack::Current); }
QString LangPackName() { return Lang::GetInstance().langPackName(); }
QString RemoteConfig() {
	const auto result = qEnvironmentVariable("AYUGRAM_REMOTE_CONFIG");
#ifdef AYUGRAM_REMOTE_ONLY
	if (result.isEmpty()) { qFatal("Start this client through remote/setup.py."); }
#endif // AYUGRAM_REMOTE_ONLY
	return result;
}
void RefreshProxy() { Core::App().refreshGlobalProxy(); }
void BadConfiguration() { Core::App().badMtprotoConfigurationError(); }
void WriteSettings() { Local::writeSettings(); }
void ResetLanguage() { Lang::CurrentCloudManager().resetToDefault(); }
void RecordRetry(int code, const QString &type, uint32 constructor) {
	Test::RecordRpcRetry(code, type, constructor);
}
std::unique_ptr<MTP::details::AbstractSocket> WebSocket(
		not_null<QThread*> thread, const MTP::ProxyData &proxy) {
	return std::make_unique<MTP::details::WebProxySocket>(thread, proxy);
}
void ApplyConfig(const MTPDconfig &data) {
	Lang::CurrentCloudManager().setSuggestedLanguage(
		qs(data.vsuggested_lang_code().value_or_empty()));
	Lang::CurrentCloudManager().setCurrentVersions(
		data.vlang_pack_version().value_or_empty(),
		data.vbase_lang_pack_version().value_or_empty());
	if (const auto prefix = data.vautoupdate_url_prefix()) {
		Local::writeAutoupdatePrefix(qs(*prefix));
	}
}

} // namespace Ayu::Remote::Runtime
