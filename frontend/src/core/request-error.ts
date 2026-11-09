/** 请求失败使用统一的中文原因，不初始化界面或路由状态。 */
import { t } from '../i18n';
export function requestErrorMessage(cause:any,status=0):string{
  const text=String(cause?.message??cause??'');
  const code=Number(status||cause?.status||text.match(/\b(400|401|403|404|408|409|413|429|500|502|503|504)\b/)?.[1]);
  const messages:Record<number,string>={400:t('shell:requestError.badRequest'),401:t('shell:requestError.sessionExpired'),403:t('shell:requestError.forbidden'),404:t('shell:requestError.notFound'),408:t('shell:requestError.timeout'),409:t('shell:requestError.conflict'),413:t('shell:requestError.tooLarge'),429:t('shell:requestError.rateLimited'),500:t('shell:requestError.serverError'),502:t('shell:requestError.badGateway'),503:t('shell:requestError.unavailable'),504:t('shell:requestError.gatewayTimeout')};
  if(/Failed to fetch|fetch failed|NetworkError|Load failed|network request failed|ERR_CONNECTION/i.test(text))return t('shell:requestError.unreachable');
  /* 正文里的 timeout 字样只在没有状态码时才算超时：500 的堆栈里常带着某个库的 timeout 参数名。 */
  if(cause?.name==='TimeoutError'||(!code&&/timed? ?out|timeout/i.test(text)))return t('shell:requestError.clientTimeout');
  if(/[㐀-鿿]/.test(text)&&!/^请求失败[（(]/.test(text))return text;
  if(messages[code])return messages[code];
  return t('shell:requestError.fallback');
}
