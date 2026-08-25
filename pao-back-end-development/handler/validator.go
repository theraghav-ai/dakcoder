package handler

import (
	validation "gitlab.cept.gov.in/it-2.0-common/api-validation"
)

func NewValidatorService() error {
	err := validation.Create()
	if err != nil {
		return err
	}
	err = validation.RegisterCustomValidation("validatePaocode", ValidatePaocode, "field %s must be of 6 characters, but received %v")
	if err != nil {
		return err
	}
	err = validation.RegisterCustomValidation("validateDdocode", ValidateDdocode, "field %s must be of 6 characters, but received %v")
	if err != nil {
		return err
	}
	err = validation.RegisterCustomValidation("validatePeriod", ValidatePeriod, "incorrect format")
	if err != nil {
		return err
	}
	err = validation.RegisterCustomValidation("validateDateTime", ValidatedatetimeFormat, "incorrect format")
	if err != nil {
		return err
	}
	return nil
}
