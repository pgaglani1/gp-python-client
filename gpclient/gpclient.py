# Copyright IBM Corp. 2015
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json, logging, requests, datetime, hmac, base64
from gettext            import NullTranslations, \
                               translation as local_translation
from babel              import Locale, negotiate_locale
from babel.dates        import format_datetime
from hashlib            import sha1
from .gptranslations    import GPTranslations
from .gpserviceaccount  import GPServiceAccount

class GPClient():
    """Handles interaction with the Globalization Pipeline (GP) service
    instance. ``serviceAccount`` must be of type ``GPServiceAccount`` and will
    be used to obtain the necessary credentials required for contacting the
    Globalization Pipeline service instance.

    The caching feature may be used to cache translated values locally
    in order to reduce the number of calls made to the GP service. The
    ``cacheTimeout`` can have the following values (in minutes):

    * ``cacheTimeout = 0``, do not cache
    * ``cacheTimeout = -1``, cache forever
    * ``cacheTimeout > 0``, store cache value for specified number of minutes

    The default ``cacheTimeout`` value is ``10`` minutes

    The type of authentication to use for requests can also be specified.
    Currently, the following are supported:

    * HMAC authentication: ``auth=GPClient.HMAC_AUTH``
    * HTTP Basic Access authentication: ``auth=GPClient.BASIC_AUTH``

    The default ``auth`` value is ``GPClient.HMAC_AUTH``. Note, at this
    time, only Reader-type accounts are allowed to use Basic authentication.
    """

    BASIC_AUTH                      = 'basic'
    HMAC_AUTH                       = 'HMAC'

    __RFC1123_FORMAT                = 'EEE, dd LLL yyyy HH:mm:ss'

    __BUNDLES_PATH                  = '/v2/bundles'

    __AUTHORIZATION_HEADER_KEY      = 'Authorization'
    __DATE_HEADER_KEY               = 'Date'

    __RESPONSE_STATUS_KEY           = 'status'
    __RESPONSE_STATUS_SUCCESS       = 'success'
    __RESPONSE_MESSAGE_KEY          = 'message'
    __RESPONSE_BUNDLES_KEY          = 'bundleIds'
    __RESPONSE_BUNDLE_KEY           = 'bundle'
    __RESPONSE_PROJECT_ID           = 'id'
    __RESPONSE_TARGET_LANGUAGES_KEY = 'targetLanguages'
    __RESPONSE_SRC_LANGUAGE_KEY     = 'sourceLanguage'
    __RESPONSE_RESOURCE_STRINGS_KEY = 'resourceStrings'
    __RESPONSE_RESOURCE_ENTRY_KEY   = 'resourceEntry'
    __RESPONSE_TRANSLATION_KEY      = 'value'
    __RESPONSE_SOURCE_VALUE_KEY     = 'sourceValue'

    __serviceAccount = None
    __cacheTimeout = 10
    __auth = None

    def __init__(self, serviceAccount, auth=HMAC_AUTH, cacheTimeout=10):
        assert isinstance(serviceAccount, GPServiceAccount), """serviceAccount
            is not of type GPServiceAccount: %s""" % serviceAccount

        self.__serviceAccount = serviceAccount
        self.__cacheTimeout = cacheTimeout
        self.__auth = auth

    def __get_language_match(self, languageCode, languageIds):
        """Compares ``languageCode`` to the provided ``languageIds`` to find
        the closest match and returns it, if a match is not found returns
        ``None``.

        e.g. if ``languageCode`` is ``en_CA`` and ``languageIds`` contains
        ``en``, the return value will be ``en``
        """
        # special case
        if languageCode == 'zh':
            return 'zh-Hans'

        # this will take care of cases such as mapping en_CA to en
        if '-' in languageCode:
            match = negotiate_locale([languageCode], languageIds, sep='-')
        else:
            match = negotiate_locale([languageCode], languageIds)

        if match:
            return match

        # handle other cases
        if '-' in languageCode:
            locale = Locale.parse(languageCode, sep='-')
        else:
            locale = Locale.parse(languageCode)

        for languageId in languageIds:
            if '-' not in languageId:
                continue

            # normalize the languageId
            nLanguageId = Locale.parse(languageId, sep='-')

            # 1. lang subtag must match
            # 2. either script or territory subtag must match AND
            #    one of them must not be None, i.e. do not allow None == None
            if locale.language == nLanguageId.language and \
                (((locale.script or nLanguageId.script) and
                (locale.script == nLanguageId.script)) or \
                (locale.territory or nLanguageId.territory) and
                (locale.territory == nLanguageId.territory)):
                    return languageId

        return None

    def __get_base_bundle_url(self):
        """Returns ``{rest api url}/{serviceInstanceId}/v2/bundles`` """
        return self.__serviceAccount.get_url() + '/' + \
            self.__serviceAccount.get_instance_id() + self.__BUNDLES_PATH

    def __get_RFC1123_date(self):
        now = datetime.datetime.utcnow()
        return format_datetime(now, self.__RFC1123_FORMAT, locale='en') + ' GMT'

    def __get_gaas_hmac_headers(self, method, url, date=None, body=None,
        secret=None, userId=None):
        """Note: this documentation was copied for the Java client for GP.

        Generate GaaS HMAC credentials used for HTTP Authorization header.
        GaaS HMAC uses HMAC SHA1 algorithm signing a message composed by:

        (HTTP method)[LF]       (in UPPERCASE)
        (Target URL)[LF]
        (RFC1123 date)[LF]
        (Request Body)

        If the request body is empty, it is simply omitted,
        the 'message' then ends with new line code [LF].

        The format for HTTP Authorization header is:

        "Authorization: GaaS-HMAC (user ID):(HMAC above)"

        For example, with user "MyUser" and secret "MySecret",
        the method "POST",
        the URL "https://example.com/gaas",
        the date "Mon, 30 Jun 2014 00:00:00 GMT",
        the body '{"param":"value"}',
        the following text to be signed will be generated:

        POST
        https://example.com/gaas
        Mon, 30 Jun 2014 00:00:00 GMT
        {"param":"value"}

        And the resulting headers are:

        Authorization: GaaS-HMAC MyUser:ONBJapYEveDZfsPFdqZHQ64GDgc=
        Date: Mon, 30 Jun 2014 00:00:00 GMT

        The HTTP Date header, matching the one included in the message
        to be signed, is required for GaaS HMAC authentication. GaaS
        authentication code checks the Date header value and if it's too old,
        it rejects the request.
        """
        if not date:
            date = self.__get_RFC1123_date()

        message = str(method) + '\n'+ \
                  str(url) + '\n' + \
                  str(date) + '\n'

        if body:
            message += str(body)

        if not secret:
            secret = self.__serviceAccount.get_password()
        secret = bytes(secret.encode('utf-8'))
        message = bytes(message.encode('utf-8'))
        digest = hmac.new(secret, message, sha1).digest()
        urlSafeHmac =  base64.b64encode(digest).strip()

        if not userId:
            userId = self.__serviceAccount.get_user_id()
        urlSafeHmac = urlSafeHmac.strip().decode('utf-8')
        authorizationValue = 'GaaS-HMAC ' + userId + ':' + urlSafeHmac

        headers = {
            self.__AUTHORIZATION_HEADER_KEY: str(authorizationValue),
            self.__DATE_HEADER_KEY: str(date)
        }

        return headers

    def __perform_rest_get_call(self, requestURL, params=None, headers=None):
        """Returns the JSON representation of the response if the response
        status was ok, returns ``None`` otherwise.
        """

        if self.__auth == self.BASIC_AUTH:
            auth = (self.__serviceAccount.get_user_id(),
                self.__serviceAccount.get_password())
        elif self.__auth == self.HMAC_AUTH:
            auth = None

            # need to prepare url by appending params to the end
            # before creating the hmac headers
            fakeRequest = requests.PreparedRequest()
            fakeRequest.prepare_url(requestURL, params=params)
            preparedUrl = fakeRequest.url

            hmacHeaders= self.__get_gaas_hmac_headers(method='GET',
                url=preparedUrl)

            if headers:
                headers.update(hmacHeaders)
            else:
                headers = hmacHeaders

        r = requests.get(requestURL, auth=auth, headers=headers, params=params)

        if r is None:
            logging.info('No response for REST GET request')
            return None

        httpStatus = r.status_code
        logging.info('HTTP status code: %s', httpStatus)

        if httpStatus == requests.codes.ok:
            jsonR = r.json()
            if jsonR:
                statusStr = 'REST response status: %s' % \
                    jsonR.get(self.__RESPONSE_STATUS_KEY)
                msgStr = 'REST response message: %s' % \
                    jsonR.get(self.__RESPONSE_MESSAGE_KEY)
                logging.info(statusStr)
                logging.info(msgStr)
                return jsonR
            else:
                logging.warning('Unable to parse JSON body.')
                logging.warning(r.text)
                return None
        else:
            logging.warning('Invalid HTTP status code.')
            logging.warning(r.text)
            return None

    def __get_bundles_data(self):
        """``GET {url}/{serviceInstanceId}/v2/bundles``

        Gets a list of bundle IDs.
        """
        url = self.__get_base_bundle_url()
        response = self.__perform_rest_get_call(requestURL=url)

        if not response:
            return None

        bundlesData = response.get(self.__RESPONSE_BUNDLES_KEY)

        return bundlesData

    def __get_bundle_data(self, bundleId):
        """``GET /{serviceInstanceId}/v2/bundles/{bundleId}``

        Gets the bundle's information.
        """
        url = self.__get_base_bundle_url() + '/' + bundleId
        response = self.__perform_rest_get_call(requestURL=url)

        if not response:
            return None

        bundleData = response.get(self.__RESPONSE_BUNDLE_KEY)

        return bundleData

    def __get_language_data(self, bundleId, languageId, fallback=False):
        """``GET /{serviceInstanceId}/v2/bundles/{bundleId}/{languageId}``

        Gets the resource strings (key/value pairs) for the language. If
        ``fallback`` is ``True``, source language value is used if translated
        value is not available.
        """
        url = self.__get_base_bundle_url() + '/' + bundleId + '/' + languageId
        params = {'fallback':'true'} if fallback else None
        response = self.__perform_rest_get_call(requestURL=url, params=params)

        if not response:
            return None

        languageData = response.get(self.__RESPONSE_RESOURCE_STRINGS_KEY)

        return languageData

    def __get_resource_entry_data(self, bundleId, languageId, resourceKey,
        fallback=False):
        """``GET /{serviceInstanceId}/v2/bundles/{bundleId}/{languageId}
        /{resourceKey}``

        Gets the resource entry information.
        """
        url = self.__get_base_bundle_url() + '/' + bundleId + '/' + languageId \
            + '/' + resourceKey
        params = {'fallback':'true'} if fallback else None
        response = self.__perform_rest_get_call(requestURL=url, params=params)

        if not response:
            return None

        resourceEntryData = response.get(self.__RESPONSE_RESOURCE_ENTRY_KEY)

        return resourceEntryData

    def __get_bundles(self):
        """Returns list of avaliable bundles """
        bundleIds = self.__get_bundles_data()

        return bundleIds if bundleIds else []

    def __has_language(self, bundleId, languageId):
        """Returns ``True`` if the bundle has the language, ``False`` otherwise
        """
        return True if self.__get_language_data(bundleId=bundleId,
            languageId=languageId) else False

    def __get_keys_map(self, bundleId, languageId, fallback=False):
        """Returns key-value pairs for the specified language.
        If fallback is ``True``, source language value is used if translated
        value is not available.
        """
        return self.__get_language_data(bundleId=bundleId,
            languageId=languageId, fallback=fallback)

    def __get_value(self, bundleId, languageId, resourceKey, fallback=False):
        """Returns the value for the key. If fallback is ``True``, source
        language value is used if translated value is not available. If the
        key is not found, returns ``None``.
        """
        resourceEntryData = self.__get_resource_entry_data(bundleId=bundleId,
            languageId=languageId, resourceKey=resourceKey, fallback=fallback)

        if not resourceEntryData:
            return None

        value = resourceEntryData.get(self.__RESPONSE_TRANSLATION_KEY)

        return value

    def get_avaliable_languages(self, bundleId):
        """Returns a list of avaliable languages in the bundle"""
        bundleData = self.__get_bundle_data(bundleId)

        if not bundleData:
            return []

        sourceLanguage = bundleData.get(self.__RESPONSE_SRC_LANGUAGE_KEY)
        languages = bundleData.get(self.__RESPONSE_TARGET_LANGUAGES_KEY)
        languages.append(sourceLanguage)

        return languages if languages else []

    def gp_translation(self, bundleId, languages):
        """Returns an instance of ``GPTranslations`` to be used for obtaining
        translations.

        ``bundleId`` is the name of the bundle to use. ``languages`` is the
        list of languages to use, with subsequent ones being fallbacks.
        For example, to fallback to Spanish if French translated values are not
        found, ``languages=['fr', 'es']``.
        """
        return self.translation(bundleId=bundleId, languages=languages)


    def translation(self, bundleId, languages, priority='gp', domain=None,
        localedir=None, class_=None, codeset=None):
        """Returns the ``Translations`` instance to be used for obtaining
        translations.

        ``bundleId`` is the name of the bundle to use.
        ``languages`` is the list of languages to use, with subsequent ones
        being fallbacks. Additionally, based on the value of ``priority``,
        local translated values can be given precedence over Globalization
        Pipeline translated values.

        For example, to fallback to Spanish if French translated values are not
        found, ``languages=['fr', 'es']``. And if ``priority=gp``,
        the fallback chain will be as follows:

        - use ``gp`` French translated value, if not found:
        - use ``local`` French translated value, if not found:
        - use ``gp`` Spanish translated value, if not found:
        - use ``local`` Spanish translated value, if not found:
        - use source value, if not found:
        - use provided key

        In order to search for local translated values, the optional parameters
        must be provided according to `gettext.translation
        <https://docs.python.org/2/library/gettext.html#gettext.translation>`_
        """

        availableLangs = self.get_avaliable_languages(bundleId)

        translations = None

        for language in languages:
            # get local translation
            localTranslations = None
            if domain:
                t = local_translation(domain=domain,
                    localedir=localedir, languages=[language], class_=class_,
                    fallback=True, codeset=codeset)

                # only use t if it's not NullTranslations - NullTranslations
                # indicates that a translation file was not found
                if t is not NullTranslations:
                    localTranslations = t

            gpTranslations = None

            # get gp translation if the bundle has the language
            match = self.__get_language_match(languageCode=language,
                languageIds=availableLangs)
            if match:
                gpTranslations = GPTranslations(bundleId=bundleId,
                    languageId=match, client=self,
                    cacheTimeout=self.__cacheTimeout)

            # create the fallback chain
            if not translations:
                # set the first translation in the chain
                if priority == 'local':
                    if not localTranslations:
                        translations = gpTranslations
                    else:
                        translations = localTranslations

                        if gpTranslations:
                            translations.add_fallback(gpTranslations)

                elif priority == 'gp':
                    if not gpTranslations:
                        translations = localTranslations
                    else:
                        translations = gpTranslations

                        if localTranslations:
                            translations.add_fallback(localTranslations)
            else:
                # add fallback in the preferred order
                if priority == 'local':
                    if localTranslations:
                        translations.add_fallback(localTranslations)
                    if gpTranslations:
                        translations.add_fallback(gpTranslations)
                elif priority == 'gp':
                    if gpTranslations:
                        translations.add_fallback(gpTranslations)
                    if localTranslations:
                        translations.add_fallback(localTranslations)

        if not translations:
            logging.warning('No translations were found for bundleID <%s> and' +
                ' languages <%s> ', bundleId, languages)
            translations = NullTranslations()

        return translations
